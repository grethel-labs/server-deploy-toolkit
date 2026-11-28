"""HTTP API and web app server."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from opstend import __version__, i18n
from opstend.adapters.base import Capability
from opstend.api.auth import RANK, Role, Session, Sessions, check_password, hash_password
from opstend.cluster import rollout
from opstend.cluster.consensus import NotLeader
from opstend.cluster.node import web_routes
from opstend.cluster.placement import ROLES
from opstend.core.jobs import Job, JobError, JobState, Step
from opstend.core.models import GuestKind, Platform
from opstend.core.policy import Policy
from opstend.deploy import analyzer
from opstend.deploy.spec import WorkloadSpec, validate
from opstend.i18n import tr
from opstend.network import graph as netgraph
from opstend.network import plantuml
from opstend.runtime import Runtime
from opstend.zorec.assistant import Mode

STATIC = Path(__file__).parent / "static"
COOKIE = "opstend_session"


# -- request models -------------------------------------------------------------


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class Lang(BaseModel):
    lang: str


class GuestJob(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class Approve(BaseModel):
    fingerprint: str | None = None


class Reject(BaseModel):
    reason: str = ""


class Explain(BaseModel):
    src: str
    dst: str
    port: int | None = None


class Ask(BaseModel):
    question: str = Field(max_length=2000)
    page: dict[str, Any] | None = None
    mode: Mode = Mode.READ
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


class Analyze(BaseModel):
    repo: str | None = None
    name: str | None = None
    files: dict[str, str] | None = None


class Deploy(BaseModel):
    spec: WorkloadSpec
    host: str
    public_ok: bool = False


class StepIn(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class JobIn(BaseModel):
    title: str = Field(max_length=200)
    target: str = Field(max_length=200)
    reason: str = Field(default="", max_length=500)
    steps: list[StepIn] = Field(min_length=1, max_length=20)


class RolloutRequest(BaseModel):
    targets: list[str] | None = None


# -- helpers --------------------------------------------------------------------


def dump(obj: Any) -> Any:
    """JSON-ready data with every stored text rendered in the reader's language."""
    return i18n.localize(_raw(obj))


def _raw(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [_raw(o) for o in obj]
    if isinstance(obj, i18n.I18nStr):
        return obj
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _raw(v) for k, v in obj.__dict__.items()}
    return obj


DEMO_REPOS = {
    "https://github.com/werkstatt/wetterstation": {
        "compose.yaml": """services:
  web:
    build: .
    ports: ["8080:8080"]
    environment:
      DATABASE_URL: postgres://wetter@db/wetter
      API_TOKEN: ${API_TOKEN}
    depends_on: [db]
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8080/health"]
  db:
    image: docker.io/library/postgres:17
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes:
  pgdata: {}
""",
        ".env.example": "API_TOKEN=\nDB_PASSWORD=\nTZ=Europe/Berlin\n",
    },
}


async def fetch_repo(url: str) -> tuple[dict[str, str], str | None]:
    """Read the few declarative files we analyze from an exact GitHub revision."""
    m = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url.strip())
    if not m:
        raise HTTPException(
            422,
            tr("Only https://github.com/<owner>/<repo> URLs are supported yet"),
        )
    owner, repo = (quote(part, safe="") for part in m.groups())
    if owner.startswith(".") or repo.startswith("."):
        raise HTTPException(422, tr("Invalid repository URL"))
    async with httpx.AsyncClient(timeout=15) as client:
        head = await client.get(f"https://api.github.com/repos/{owner}/{repo}/commits/HEAD")
        if head.status_code != 200:
            raise HTTPException(
                404,
                tr("Repository not found or not public"),
            )
        sha = head.json()["sha"]
        files: dict[str, str] = {}
        for path in analyzer.INTERESTING:
            r = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}")
            if r.status_code == 200 and len(r.content) < 256 * 1024:
                files[path] = r.text
    return files, sha


def build_app(rt: Runtime) -> FastAPI:
    sessions = Sessions()
    background: set[asyncio.Task[Any]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await rt.start()
        yield
        await rt.stop()

    app = FastAPI(
        title="OpsTend",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.exception_handler(NotLeader)
    async def leadership_changing(_request: Request, _exc: NotLeader) -> JSONResponse:
        return JSONResponse(
            {"detail": tr("The cluster is electing a new leader. Try again in a few seconds.")},
            status_code=503,
            headers={"Retry-After": "3"},
        )

    @app.middleware("http")
    async def security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        session = sessions.get(request.cookies.get(COOKIE))
        token = i18n.current.set(
            i18n.pick(request.headers.get("accept-language"), session.lang if session else None)
        )
        try:
            if (
                request.url.path.startswith("/api/")
                and request.method not in ("GET", "HEAD")
                and request.url.path not in ("/api/session", "/api/setup", "/api/cluster/join")
            ):
                if session is None or request.headers.get("x-csrf-token") != session.csrf:
                    return JSONResponse({"detail": "CSRF token missing or wrong"}, 403)
            response = await call_next(request)
        finally:
            i18n.current.reset(token)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    def current(request: Request) -> Session:
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None:
            raise HTTPException(401, tr("Please sign in"))
        return s

    def role(minimum: Role) -> Callable[[Request], Session]:
        def check(request: Request) -> Session:
            s = current(request)
            if RANK[s.role] < RANK[minimum]:
                raise HTTPException(403, tr("Your role does not allow this"))
            return s

        return check

    viewer, operator, admin = role(Role.VIEWER), role(Role.OPERATOR), role(Role.ADMIN)

    def session_json(s: Session) -> dict[str, Any]:
        return {
            "user": s.user,
            "name": s.name,
            "role": s.role,
            "csrf": s.csrf,
            "lang": s.lang,
            "demo": rt.demo,
            "version": __version__,
            "site": rt.store.meta("site.name") or "OpsTend",
        }

    def set_cookie(resp: Response, s: Session, request: Request) -> None:
        resp.set_cookie(
            COOKIE,
            s.token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )

    # -- session ---------------------------------------------------------

    @app.get("/api/session")
    async def get_session(request: Request, response: Response) -> dict[str, Any]:
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None and rt.demo:
            s = sessions.create(
                "demo", "Demo", Role.ADMIN, i18n.pick(request.headers.get("accept-language"))
            )
            set_cookie(response, s, request)
        if s is None:
            return {
                "user": None,
                "setup": not rt.store.list("user"),
                "demo": rt.demo,
                "version": __version__,
            }
        return session_json(s)

    @app.post("/api/session")
    async def login(body: Login, request: Request, response: Response) -> dict[str, Any]:
        addr = request.client.host if request.client else "?"
        if not sessions.allow_login(addr):
            raise HTTPException(
                429,
                tr("Too many attempts. Wait five minutes."),
            )
        user = rt.store.get("user", body.username.lower())
        if not user or not check_password(body.password, user["password"]):
            rt.audit.record(body.username, "session.login", "user", "refused")
            raise HTTPException(401, tr("Wrong user name or password"))
        s = sessions.create(user["id"], user["name"], Role(user["role"]), user.get("lang", "de"))
        set_cookie(response, s, request)
        rt.audit.record(s.user, "session.login", "user")
        return session_json(s)

    @app.post("/api/setup")
    async def setup(body: Login, request: Request, response: Response) -> dict[str, Any]:
        if rt.store.list("user"):
            raise HTTPException(409, tr("Setup is already done"))
        try:
            hashed = hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(422, tr("Use at least 12 characters")) from exc
        uid = body.username.lower()
        await rt.write(
            {
                "op": "put",
                "kind": "user",
                "id": uid,
                "data": {
                    "id": uid,
                    "name": body.username,
                    "role": Role.ADMIN,
                    "password": hashed,
                    "lang": "de",
                },
            }
        )
        s = sessions.create(uid, body.username, Role.ADMIN)
        set_cookie(response, s, request)
        rt.audit.record(uid, "setup.admin", "user")
        return session_json(s)

    @app.delete("/api/session")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        sessions.drop(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.put("/api/session/lang")
    async def set_lang(body: Lang, s: Session = Depends(viewer)) -> dict[str, Any]:
        if body.lang not in i18n.languages():
            raise HTTPException(422, "unsupported language")
        s.lang = body.lang
        return session_json(s)

    # -- overview --------------------------------------------------------

    @app.get("/api/i18n")
    async def i18n_languages() -> dict[str, Any]:
        return {"default": i18n.DEFAULT, "languages": i18n.languages()}

    @app.get("/api/i18n/{lang}")
    async def i18n_catalog(lang: str) -> Response:
        if lang not in i18n.languages():
            raise HTTPException(404, "unknown language")
        return JSONResponse(i18n.catalog(lang), headers={"Cache-Control": "public, max-age=300"})

    @app.get("/api/overview")
    async def overview(_: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        hosts, guests, services = f.hosts(), f.guests(), f.services()
        jobs = rt.engine.list()
        sites: dict[str, dict[str, Any]] = {}
        for h in hosts:
            site = sites.setdefault(
                h.site, {"name": h.site, "hosts": 0, "ok": 0, "workloads": 0, "stale": 0}
            )
            site["hosts"] += 1
            site["ok"] += int(h.health in ("ok", "warning"))
            site["stale"] += int(bool(h.observed and h.observed.is_stale()))
            site["workloads"] += sum(1 for g in guests if g.host_id == h.id and not g.template)
        cap = {
            "cpu_cores": sum(h.capacity.cpu_cores for h in hosts),
            "memory_mb": sum(h.capacity.memory_mb for h in hosts),
            "memory_used_mb": sum(h.usage.memory_mb or 0 for h in hosts),
            "disk_gb": sum(h.capacity.disk_gb for h in hosts),
            "disk_used_gb": sum(h.usage.disk_gb or 0 for h in hosts),
            "measured": sum(1 for h in hosts if h.usage.memory_mb is not None),
        }
        findings = f.findings()
        ups = f.updates()
        return {
            "incidents": [dump(i) for i in rt.incidents if i.state != "resolved"],
            "unhealthy": [dump(s) for s in services if s.health in ("critical", "warning")],
            "decisions": dump([j for j in jobs if j.state == JobState.AWAITING_APPROVAL]),
            "working": dump(
                [
                    j
                    for j in jobs
                    if j.state in (JobState.RUNNING, JobState.VERIFYING, JobState.SCHEDULED)
                ]
            ),
            "counts": {
                "hosts": len(hosts),
                "hosts_ok": sum(h.health == "ok" for h in hosts),
                "guests": sum(not g.template for g in guests),
                "guests_running": sum(g.state == "running" for g in guests),
                "services": len(services),
                "services_ok": sum(s.health == "ok" for s in services),
                "devices": len(f.devices()),
                "clients": len(f.clients()),
                "findings_high": sum(x.severity in ("critical", "high") for x in findings),
                "updates": len(ups),
                "updates_security": sum(u.security for u in ups),
            },
            "capacity": cap,
            "sites": list(sites.values()),
            "insights": [i.as_dict() for i in f.insights[:4]],
            "activity": rt.audit.entries(limit=8),
            "cluster": {**rt.raft.status(), "nodes": len(f.nodes())},
        }

    # -- compute ---------------------------------------------------------

    def caps(guest_kind: str, platform: str) -> list[str]:
        if platform == Platform.PROXMOX:
            base = {c.value for c in Capability}
            return sorted(base - ({"suspend"} if guest_kind == GuestKind.LXC else set()))
        if guest_kind == GuestKind.VM:
            return ["backup", "clone", "migrate", "power", "snapshot"]
        return ["backup", "clone", "power"]

    @app.get("/api/hosts")
    async def hosts(_: Session = Depends(viewer)) -> list[dict[str, Any]]:
        f = rt.fleet()
        guests = f.guests()
        ups = f.updates()
        out = []
        for h in f.hosts():
            d = dump(h)
            d["guests"] = sum(1 for g in guests if g.host_id == h.id and not g.template)
            d["running"] = sum(1 for g in guests if g.host_id == h.id and g.state == "running")
            d["updates"] = sum(1 for u in ups if u.target == h.id)
            d["stale"] = bool(h.observed and h.observed.is_stale())
            out.append(d)
        return out

    @app.get("/api/hosts/{host_id:path}")
    async def host(host_id: str, _: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        h = next((x for x in f.hosts() if x.id == host_id), None)
        if h is None:
            raise HTTPException(404, "host not found")
        return {
            "host": dump(h),
            "guests": dump([g for g in f.guests() if g.host_id == host_id]),
            "bridges": [b.__dict__ for b in f.bridges() if b.host_id == host_id],
            "updates": dump([u for u in f.updates() if u.target == host_id]),
            "findings": dump([x for x in f.findings() if x.target == host_id]),
            "node": dump(next((n for n in f.nodes() if n.host_id == host_id), None)),
            "insights": [i.as_dict() for i in f.insights if host_id in i.objects],
        }

    @app.get("/api/guests")
    async def guests(_: Session = Depends(viewer)) -> list[dict[str, Any]]:
        f = rt.fleet()
        platforms = {h.id: h.platform for h in f.hosts()}
        names = {h.id: h.name for h in f.hosts()}
        out = []
        for g in f.guests():
            d = dump(g)
            d["host_name"] = names.get(g.host_id, g.host_id)
            d["platform"] = platforms.get(g.host_id)
            d["capabilities"] = caps(g.kind, platforms.get(g.host_id, ""))
            out.append(d)
        return out

    @app.get("/api/guests/{guest_id:path}")
    async def guest(guest_id: str, _: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        g = next((x for x in f.guests() if x.id == guest_id), None)
        if g is None:
            raise HTTPException(404, "guest not found")
        h = next((x for x in f.hosts() if x.id == g.host_id), None)
        return {
            "guest": dump(g),
            "host": dump(h),
            "capabilities": caps(g.kind, h.platform if h else ""),
            "service": dump(next((s for s in f.services() if s.guest_id == guest_id), None)),
            "jobs": dump([j for j in rt.engine.list() if j.target == guest_id][:10]),
            "insights": [i.as_dict() for i in f.insights if guest_id in i.objects],
        }

    @app.post("/api/guests/{guest_id:path}/jobs")
    async def guest_job(guest_id: str, body: GuestJob, s: Session = Depends(operator)) -> Any:
        allowed = {
            "power",
            "snapshot",
            "snapshot_rollback",
            "snapshot_delete",
            "backup",
            "clone",
            "migrate",
        }
        if body.action not in allowed:
            raise HTTPException(422, "unknown action")
        params = {**body.params, "guest": guest_id}
        if body.action == "clone":
            params = {
                "template": guest_id,
                "name": body.params.get("name", "neu"),
                "host": body.params.get("host"),
            }
        job = Job(
            title="",
            category="power",
            target=guest_id,
            requested_by=s.user,
            steps=[Step(action=f"guest.{body.action}", params=params)],
        )
        job = await rt.engine.preview(job)
        job.title = job.steps[0].effect
        await rt.engine._save(job)
        return dump(job)

    @app.get("/api/services")
    async def services(_: Session = Depends(viewer)) -> list[dict[str, Any]]:
        f = rt.fleet()
        guests = {g.id: g for g in f.guests()}
        names = {h.id: h.name for h in f.hosts()}
        out = []
        for svc in f.services():
            d = dump(svc)
            g = guests.get(svc.guest_id or "")
            d["guest"] = dump(g)
            d["host_name"] = names.get(g.host_id) if g else None
            out.append(d)
        return out

    # -- network ---------------------------------------------------------

    @app.get("/api/network")
    async def network(_: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        return {
            "devices": dump(f.devices()),
            "networks": dump(f.networks()),
            "clients": dump(f.clients()),
            "firewall": dump(f.firewall()),
            "tunnels": dump(f.tunnels()),
            "publications": dump(f.publications()),
            "insights": [i.as_dict() for i in f.insights],
        }

    @app.get("/api/network/topology")
    async def topology(
        layers: str = "physical",
        focus: str | None = None,
        clients: bool = False,
        dark: bool = False,
        _: Session = Depends(viewer),
    ) -> dict[str, Any]:
        f = rt.fleet()
        wanted = {x for x in layers.split(",") if x in plantuml.LAYERS} or {"physical"}
        source = plantuml.render(
            f.graph, wanted, f.insights, focus=focus, show_clients=clients, dark=dark
        )
        hooks = {
            plantuml.hook_id(v.id): {"id": v.id, "kind": v.kind, "name": v.name}
            for v in f.graph.vertices.values()
        }
        return {"source": source, "hooks": hooks}

    @app.post("/api/network/explain")
    async def explain(body: Explain, _: Session = Depends(viewer)) -> dict[str, Any]:
        result = netgraph.explain(rt.fleet().graph, body.src, body.dst, body.port)
        return dump(result)

    @app.post("/api/network/insights/{insight_id}/fix")
    async def fix(insight_id: str, s: Session = Depends(operator)) -> Any:
        insight = next((i for i in rt.fleet().insights if i.finding().id == insight_id), None)
        if insight is None or not insight.fix or insight.fix["action"] not in rt.registry.names():
            raise HTTPException(
                404,
                tr("No automatic fix for this insight"),
            )
        params = {k: v for k, v in insight.fix.items() if k not in ("summary", "action")}
        job = await rt.engine.preview(
            Job(
                title=insight.fix["summary"],
                category="network",
                target=insight.objects[0],
                reason=insight.title,
                requested_by=s.user,
                steps=[Step(action=insight.fix["action"], params=params)],
            )
        )
        return dump(job)

    # -- security and updates -------------------------------------------

    @app.get("/api/security")
    async def security_view(_: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        findings = sorted(f.findings(), key=lambda x: (order[x.severity], -(x.epss or 0)))
        return {
            "findings": dump(findings),
            "network": [i.as_dict() for i in f.insights if i.code == "segmentation-open"],
        }

    def rollout_targets(only: list[str] | None) -> tuple[list[rollout.Target], dict[str, str]]:
        f = rt.fleet()
        by_target: dict[str, list[Any]] = {}
        for u in f.updates():
            by_target.setdefault(u.target, []).append(u)
        voters = {n.host_id for n in f.nodes() if n.voter}
        devices = {d.id: d for d in f.devices()}
        mac_to_id = {d.mac: d.id for d in devices.values()}
        hosts = {h.id: h for h in f.hosts()}
        nodes = {n.id: n for n in f.nodes()}
        uplink_of: dict[str, str] = {}
        for e in f.graph.edges:
            if e.kind == "cable" and e.b in hosts and e.a in devices:
                uplink_of[e.b] = e.a
        targets, actions = [], {}
        for tid, ups in by_target.items():
            if only and tid not in only:
                continue
            if tid in devices:
                d = devices[tid]
                kind = {
                    "gateway": rollout.TargetKind.UNIFI_GATEWAY,
                    "switch": rollout.TargetKind.UNIFI_SWITCH,
                    "ap": rollout.TargetKind.UNIFI_AP,
                }.get(d.kind, rollout.TargetKind.UNIFI_SWITCH)
                dep = [mac_to_id[d.uplink_mac]] if d.uplink_mac in mac_to_id else []
                targets.append(
                    rollout.Target(
                        id=tid,
                        name=d.name,
                        kind=kind,
                        depends_on=dep,
                        current=d.firmware,
                        available=d.upgrade_to or "",
                        criticality={"ap": 0, "switch": 2}.get(d.kind, 3),
                    )
                )
                actions[tid] = "unifi.device_upgrade"
            elif tid in hosts:
                h = hosts[tid]
                targets.append(
                    rollout.Target(
                        id=tid,
                        name=h.name,
                        kind=rollout.TargetKind.PROXMOX
                        if h.platform == "proxmox"
                        else rollout.TargetKind.LINUX,
                        voter=tid in voters,
                        site=h.site,
                        depends_on=[uplink_of[tid]] if tid in uplink_of else [],
                        reboot=any(u.reboot for u in ups),
                        criticality=2 if tid in voters else 1,
                    )
                )
                actions[tid] = "host.patch"
            elif tid in nodes:
                targets.append(
                    rollout.Target(
                        id=tid,
                        name=f"OpsTend {nodes[tid].name}",
                        kind=rollout.TargetKind.OPSTEND,
                        current=ups[0].current,
                        available=ups[0].available,
                        reboot=False,
                        criticality=0,
                    )
                )
                actions[tid] = "self.update"
            else:
                targets.append(
                    rollout.Target(
                        id=tid,
                        name=tid.split("/")[-1],
                        kind=rollout.TargetKind.APP,
                        reboot=False,
                        criticality=1,
                    )
                )
                actions[tid] = "app.update"
        return targets, actions

    @app.get("/api/updates")
    async def updates(_: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        targets, _actions = rollout_targets(None)
        try:
            waves = rollout.plan(
                targets,
                rollout.Constraints(
                    voters_total=len(rt.raft.voters) if len(rt.raft.voters) > 1 else 3
                ),
            )
            plan_error = None
        except rollout.PlanError as exc:
            waves, plan_error = [], str(exc)
        names = {tg.id: tg.name for tg in targets}
        return {
            "updates": dump(f.updates()),
            "targets": dump(targets),
            "waves": dump(waves),
            "names": names,
            "plan_error": plan_error,
            "window": dump(rt.policy.window),
            "next_window": rt.policy.window.next_open()[0].isoformat(),
        }

    @app.post("/api/rollouts")
    async def start_rollout(body: RolloutRequest, s: Session = Depends(operator)) -> Any:
        targets, actions_for = rollout_targets(body.targets)
        try:
            waves = rollout.plan(targets, rollout.Constraints(voters_total=3))
        except rollout.PlanError as exc:
            raise HTTPException(409, str(exc)) from exc
        names = {tg.id: tg.name for tg in targets}
        steps = []
        for w in waves:
            for tid in w.targets:
                action = actions_for[tid]
                if action == "host.patch":
                    step = Step(action=action, params={"host": tid, "security_only": False})
                elif action == "unifi.device_upgrade":
                    step = Step(action=action, params={"device": tid})
                elif action == "self.update":
                    target = next(tg for tg in targets if tg.id == tid)
                    step = Step(
                        action="opstend.self_update",
                        params={"node": tid, "version": target.available},
                    )
                else:
                    continue  # application updates run through their own deploy jobs
                step.title = tr(
                    "Wave {w_index_1}: {names_tid}", w_index_1=w.index + 1, names_tid=names[tid]
                )
                steps.append(step)
        job = await rt.engine.preview(
            Job(
                title=tr("Coordinated update of {len_steps} systems", len_steps=len(steps)),
                category="upgrade",
                target="fleet",
                requested_by=s.user,
                steps=steps,
                reason=tr("Waves keep quorum and update leaves before core devices"),
            )
        )
        return dump(job)

    # -- jobs and events ---------------------------------------------------

    @app.get("/api/jobs")
    async def jobs(open: bool = False, _: Session = Depends(viewer)) -> Any:
        return dump(rt.engine.list(open_only=open))

    @app.post("/api/jobs")
    async def create_job(body: JobIn, s: Session = Depends(operator)) -> Any:
        unknown = [st.action for st in body.steps if st.action not in rt.registry.names()]
        if unknown:
            raise HTTPException(422, f"unknown action {unknown[0]}")
        job = await rt.engine.preview(
            Job(
                title=body.title,
                category=body.steps[0].action.split(".")[0],
                target=body.target,
                reason=body.reason,
                requested_by=s.user,
                steps=[Step(action=st.action, params=st.params) for st in body.steps],
            )
        )
        return dump(job)

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, _: Session = Depends(viewer)) -> dict[str, Any]:
        try:
            j = rt.engine.get(job_id)
        except JobError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"job": dump(j), "events": rt.audit.entries(job_id=job_id)}

    @app.post("/api/jobs/{job_id}/approve")
    async def approve(job_id: str, body: Approve, s: Session = Depends(operator)) -> Any:
        try:
            j = await rt.engine.approve(job_id, s.user, body.fingerprint)
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc
        task = asyncio.create_task(rt.engine.run(job_id))
        background.add(task)
        task.add_done_callback(background.discard)
        return dump(j)

    @app.post("/api/jobs/{job_id}/reject")
    async def reject(job_id: str, body: Reject, s: Session = Depends(operator)) -> Any:
        try:
            return dump(await rt.engine.reject(job_id, s.user, body.reason))
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/events")
    async def events(
        target: str | None = None, limit: int = 100, _: Session = Depends(viewer)
    ) -> Any:
        return rt.audit.entries(limit=min(limit, 500), target=target)

    @app.get("/api/stream")
    async def stream(request: Request, _: Session = Depends(viewer)) -> StreamingResponse:
        q = rt.subscribe()

        async def gen() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(q.get(), 15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                rt.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- cluster and policy ---------------------------------------------

    @app.get("/api/cluster")
    async def cluster(_: Session = Depends(viewer)) -> dict[str, Any]:
        f = rt.fleet()
        placement = json.loads(rt.store.meta("placement") or "{}")
        return {
            "raft": rt.raft.status(),
            "nodes": dump(f.nodes()),
            "placement": placement,
            "roles": [{"name": r.name, "description": r.description} for r in ROLES],
            "candidates": rt.store.list("candidate"),
        }

    @app.post("/api/cluster/tokens")
    async def join_token(s: Session = Depends(admin)) -> dict[str, Any]:
        try:
            token = await rt.issue_join_token()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        rt.audit.record(s.user, "cluster.token", "cluster")
        url = "https://github.com/grethel-labs/opstend/releases/latest/download/install.sh"
        return {
            "token": token,
            "valid_minutes": 30,
            "command": f"curl -fsSL {url} | sudo sh -s -- --join {token}",
        }

    @app.get("/api/policy")
    async def get_policy(_: Session = Depends(viewer)) -> Any:
        return dump(rt.policy)

    @app.put("/api/policy")
    async def put_policy(body: Policy, s: Session = Depends(admin)) -> Any:
        await rt.save_policy(body)
        rt.audit.record(s.user, "policy.update", "policy", detail={"kill_switch": body.kill_switch})
        return dump(body)

    # -- zorec and deployment -------------------------------------------

    @app.post("/api/zorec")
    async def zorec(body: Ask, s: Session = Depends(viewer)) -> Any:
        mode = body.mode if RANK[s.role] >= RANK[Role.OPERATOR] else Mode.READ
        answer = await rt.zorec().ask(
            body.question, page=body.page, mode=mode, lang=s.lang, history=body.history
        )
        return dump(answer)

    @app.post("/api/deploy/analyze")
    async def analyze(body: Analyze, _: Session = Depends(operator)) -> dict[str, Any]:
        revision = None
        if body.files is not None:
            files = body.files
        elif body.repo and body.repo.rstrip("/") in DEMO_REPOS:
            files, revision = DEMO_REPOS[body.repo.rstrip("/")], "demo"
        elif body.repo:
            files, revision = await fetch_repo(body.repo)
        else:
            raise HTTPException(422, tr("Give a repository URL"))
        name = body.name or (body.repo or "app").rstrip("/").rsplit("/", 1)[-1]
        spec = analyzer.analyze(files, name, body.repo, revision)
        f = rt.fleet()
        candidates = [h for h in f.hosts() if h.platform == "linux" and h.site != "Extern"]
        best = max(
            candidates, key=lambda h: h.capacity.memory_mb - (h.usage.memory_mb or 0), default=None
        )
        return {
            "spec": dump(spec),
            "violations": dump(validate(spec)),
            "files": sorted(files),
            "suggested_host": best.id if best else None,
            "hosts": [{"id": h.id, "name": h.name, "os": h.os} for h in candidates],
        }

    @app.post("/api/deploy")
    async def deploy(body: Deploy, s: Session = Depends(operator)) -> Any:
        problems = [v for v in validate(body.spec, allow_public=body.public_ok) if v.blocking]
        if problems:
            raise HTTPException(422, "; ".join(f"{v.service}: {v.message}" for v in problems))
        job = await rt.engine.preview(
            Job(
                title="",
                category="deploy",
                target=f"workload/{body.spec.name}",
                requested_by=s.user,
                reason=body.spec.source or "",
                steps=[
                    Step(
                        action="deploy.apply",
                        params={"spec": body.spec.model_dump(mode="json"), "host": body.host},
                    )
                ],
            )
        )
        job.title = job.steps[0].effect
        await rt.engine._save(job)
        return dump(job)

    web_routes(app, rt)

    # -- web app -----------------------------------------------------------

    # Serve only files that exist in the built web app, by exact name.
    static_files = (
        {str(f.relative_to(STATIC)): f for f in STATIC.rglob("*") if f.is_file()}
        if STATIC.is_dir()
        else {}
    )

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404)
        file = static_files.get(path)
        if file is not None:
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if path.startswith("assets/")
                else {}
            )
            return FileResponse(file, headers=headers)
        if "index.html" in static_files:
            return FileResponse(static_files["index.html"], headers={"Cache-Control": "no-store"})
        return JSONResponse({"detail": "web app not built; run npm run build in web/"}, 404)

    return app
