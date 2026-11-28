"""Wire the parts of a node together and run its background loops."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import ssl
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from opstend import __version__, actions, i18n
from opstend.cluster import discovery, placement
from opstend.cluster.consensus import NotLeader, RaftConfig, RaftNode, RaftStorage, Role
from opstend.cluster.enrollment import ClusterCA
from opstend.cluster.node import Identity, cluster_app
from opstend.cluster.transport import HttpTransport, LocalTransport
from opstend.config import Config, read_secret
from opstend.core.audit import AuditLog
from opstend.core.jobs import ActionRegistry, Engine, Job
from opstend.core.models import Node, NodeState, Resources, Usage
from opstend.core.policy import Governor, Policy
from opstend.core.store import Store
from opstend.integrations import Integrations
from opstend.selfmanage.health import ProbeResult
from opstend.selfmanage.reconciler import Incident, Observations, Reconciler
from opstend.state import Fleet
from opstend.zorec.assistant import Zorec
from opstend.zorec.providers import Provider, ProviderError, from_settings

log = logging.getLogger("opstend")

UI_ACTIONS = ["deploy.analyze", "rollout.plan"]


def local_resources() -> tuple[Resources, Usage]:
    """CPU and memory of this machine, for role placement."""
    cpus = os.cpu_count() or 1
    total = used = 0
    try:
        from opstend.adapters.linux import parse_meminfo

        total, used = parse_meminfo(Path("/proc/meminfo").read_text())
    except OSError:
        pass
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    return (
        Resources(cpu_cores=cpus, memory_mb=total),
        Usage(cpu_pct=round(min(100.0, load / cpus * 100), 1), memory_mb=used),
    )


class _QuietServer(uvicorn.Server):
    """Second server in the same process: leave signal handling to the web server."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Runtime:
    def __init__(self, config: Config, *, demo: bool = False, demo_delay: float = 0.6) -> None:
        self.config = config
        self.demo = demo
        data = Path(config.data_dir)
        if not demo:
            data.mkdir(parents=True, exist_ok=True)
        self.store = Store(":memory:" if demo else data / "state.db")
        self.identity: Identity | None = None if demo else Identity(data)
        self.audit = AuditLog(":memory:" if demo else data / "audit.db")
        voters = config.cluster.voters or [config.node.id]
        self.raft = RaftNode(
            config.node.id,
            voters,
            LocalTransport(),
            self.store,
            storage=RaftStorage(":memory:" if demo else data / "raft.db"),
            config=RaftConfig(election_min=0.2, election_max=0.4)
            if len(voters) == 1
            else RaftConfig(),
        )
        self.registry = ActionRegistry()
        self.engine = Engine(
            self.store,
            self.audit,
            self.registry,
            term=lambda: self.raft.term,
            is_leader=lambda: self.raft.role == Role.LEADER,
            writer=self.write,
        )
        if demo:
            from opstend.demo import fixtures

            fixtures.load(self.store)
            self.backend: actions.Backend = actions.DemoBackend(self.store, demo_delay)
        else:
            self.integrations = Integrations.from_config(config)
            self.backend = actions.LiveBackend(
                self.store,
                self.integrations.compute,
                self.integrations.unifi,
                self.integrations.linux,
            )
        actions.register(self.registry, self.store, self.backend)
        if isinstance(self.backend, actions.LiveBackend):
            self.backend.node_update = self._node_update
        self._policy_raw: str | None = None
        self.governor = Governor(Policy())
        self.reconciler = Reconciler(self.governor, self.engine)
        self.incidents: list[Incident] = []
        self.probes: dict[str, list[ProbeResult]] = {}
        self.provider: Provider | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self.started_at = datetime.now(UTC)
        self.engine.on_change(
            lambda job: self.publish({"type": "job", "id": job.id, "state": job.state})
        )
        self.audit.subscribe(lambda e: self.publish({"type": "event", "entry": e}))
        try:
            settings = config.zorec.model_dump()
            settings["api_key"] = read_secret(config.zorec.api_key_file)
            self.provider = from_settings(settings)
        except (ProviderError, OSError, KeyError) as exc:
            log.warning("zorec provider unavailable: %s", exc)

    # -- state -----------------------------------------------------------

    def fleet(self) -> Fleet:
        return Fleet(self.store)

    def zorec(self) -> Zorec:
        return Zorec(self.fleet(), self.engine, self.provider, self.registry.names() + UI_ACTIONS)

    @property
    def policy(self) -> Policy:
        """The replicated policy; followers see changes as soon as they are committed."""
        raw = self.store.meta("policy")
        if raw != self._policy_raw:
            self._policy_raw = raw
            self.governor.policy = Policy.model_validate_json(raw) if raw else Policy()
        return self.governor.policy

    async def save_policy(self, policy: Policy) -> None:
        await self.write({"op": "meta", "key": "policy", "value": policy.model_dump_json()})
        self.governor.policy = policy
        self._policy_raw = self.store.meta("policy")

    async def write(self, command: dict[str, Any]) -> Any:
        if self.raft.role != Role.LEADER and self.raft.leader_id is None:
            # Before the first election finishes, wait briefly instead of failing.
            for _ in range(40):
                await asyncio.sleep(0.05)
                if self.raft.role == Role.LEADER or self.raft.leader_id:
                    break
        if self.raft.role == Role.LEADER:
            return await self.raft.propose(command)
        return await self._forward("/cluster/propose", command)

    async def _forward(self, path: str, body: dict[str, Any]) -> Any:
        """Send a write or heartbeat to the current leader over mutual TLS."""
        leader = self.raft.leader_id
        peers = json.loads(self.store.meta("peers") or "{}")
        if not leader or leader not in peers or self.identity is None:
            raise NotLeader(leader)
        async with httpx.AsyncClient(verify=self.identity.client_context(), timeout=10) as c:
            resp = await c.post(f"{peers[leader]}{path}", json=body)
        if resp.status_code != 200:
            raise NotLeader(leader)
        return resp.json().get("result")

    # -- events ----------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(json.loads(json.dumps(event, default=str)))

    # -- loops -----------------------------------------------------------

    async def start(self) -> None:
        if self.identity is not None:
            await self._cluster_setup()
        self._tasks.append(asyncio.create_task(self.raft.run()))
        self._tasks.append(asyncio.create_task(self._loop(self._run_jobs, 1.0)))
        self._tasks.append(asyncio.create_task(self._loop(self._reconcile, 20.0)))
        self._tasks.append(asyncio.create_task(self._loop(self._membership, 10.0)))
        if not self.demo:
            self._tasks.append(asyncio.create_task(self._loop(self._collect, 60.0)))
            self._tasks.append(asyncio.create_task(self._loop(self._scan, 6 * 3600.0)))
            if self.config.discovery_subnets:
                self._tasks.append(asyncio.create_task(self._loop(self._discover, 900.0)))
            with contextlib.suppress(OSError):
                self._beacons = await discovery.listen_beacons(self._on_beacon)
        if self.demo:
            self._tasks.append(asyncio.create_task(self._loop(self._demo_tick, 5.0)))
        for _ in range(40):
            if self.raft.role == Role.LEADER:
                break
            await asyncio.sleep(0.05)
        if self.demo:
            await self._seed_demo_jobs()

    async def _cluster_setup(self) -> None:
        """Create or join the cluster, then serve Raft on the cluster port with mutual TLS."""
        if self.identity is None:
            return
        cfg = self.config
        token_file = Path(cfg.cluster.join_token_file)
        if not self.identity.exists:
            info = {
                "name": cfg.node.name,
                "address": cfg.node.address,
                "site": cfg.node.site,
                "labels": cfg.node.labels,
                "capacity": local_resources()[0].model_dump(),
                "version": __version__,
                "cluster_port": int(cfg.listen.cluster.rpartition(":")[2]),
                "addresses": [cfg.node.address],
            }
            if token_file.exists():
                data = await self.identity.join(token_file.read_text().strip(), cfg.node.id, info)
                self.raft.voters = list(data["voters"])
                self.store.set_meta("peers", json.dumps(data["peers"]))
                token_file.unlink()
                log.info("joined cluster; leader set has %s voters", len(data["voters"]))
            else:
                self.identity.bootstrap(cfg.cluster.name, cfg.node.id, [cfg.node.address])
                log.info("created a new cluster %s", cfg.cluster.name)
        peers = {**json.loads(self.store.meta("peers") or "{}"), **cfg.cluster.peers}
        self.raft.transport = HttpTransport(peers, self.identity.client_context())
        host, _, port = cfg.listen.cluster.rpartition(":")
        server = _QuietServer(
            uvicorn.Config(
                cluster_app(self),
                host=host or "0.0.0.0",  # noqa: S104 - mTLS only
                port=int(port),
                log_level="warning",
                lifespan="off",
                ssl_keyfile=str(self.identity.path("node.key")),
                ssl_certfile=str(self.identity.path("node.crt")),
                ssl_ca_certs=str(self.identity.path("ca.crt")),
                ssl_cert_reqs=ssl.CERT_REQUIRED,
            )
        )
        self._cluster_server = server
        self._tasks.append(asyncio.create_task(server.serve()))
        self._tasks.append(asyncio.create_task(self._heartbeat()))

    async def _heartbeat(self) -> None:
        """Followers report to the leader; the leader refreshes the peer list for Raft."""
        while True:
            try:
                peers = {
                    **json.loads(self.store.meta("peers") or "{}"),
                    **self.config.cluster.peers,
                }
                if isinstance(self.raft.transport, HttpTransport):
                    self.raft.transport.peers = peers
                self.raft.learners = [p for p in peers if p not in self.raft.voters]
                if self.raft.role != Role.LEADER and self.raft.leader_id:
                    await self._forward(
                        "/cluster/heartbeat",
                        {
                            "id": self.config.node.id,
                            "version": __version__,
                            "usage": local_resources()[1].model_dump(),
                        },
                    )
            except Exception as exc:
                log.debug("heartbeat failed: %s", exc)
            await asyncio.sleep(3 if self.raft.role == Role.LEADER else 10)

    async def _node_update(self, node_id: str, version: str) -> dict[str, Any]:
        """Ask one node to update itself; the coordinated rollout calls this per wave."""
        peers = json.loads(self.store.meta("peers") or "{}")
        if node_id not in peers or self.identity is None:
            raise RuntimeError(f"no cluster address for {node_id}")
        async with httpx.AsyncClient(verify=self.identity.client_context(), timeout=960) as c:
            resp = await c.post(f"{peers[node_id]}/cluster/self-update", json={"version": version})
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def issue_join_token(self) -> str:
        ca = self.identity.ca() if self.identity else None
        if ca is None:
            raise RuntimeError("only the node that created the cluster can issue join tokens")
        address = f"{self.config.node.address}:{self.config.listen.web.rpartition(':')[2]}"
        issued = ca.issue_token(address=address)
        secret = ClusterCA.parse_token(issued.token)[1]
        await self.write(
            {
                "op": "put",
                "kind": "join_token",
                "id": ClusterCA.secret_hash(issued.token),
                "data": {"secret": secret.hex(), "expires": issued.expires_at.isoformat()},
            }
        )
        return issued.token

    async def stop(self) -> None:
        beacons = getattr(self, "_beacons", None)
        if beacons is not None:
            beacons.close()
        server = getattr(self, "_cluster_server", None)
        if server is not None:
            server.should_exit = True
            server.force_exit = True
            await asyncio.sleep(0.2)
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), 5)

    async def _loop(self, fn: Callable[[], Awaitable[None]], every: float) -> None:
        # Background work renders texts in English; stored templates translate on read.
        i18n.current.set(i18n.DEFAULT)
        while True:
            try:
                if self.raft.role == Role.LEADER:
                    await fn()
            except Exception:
                log.exception("background task %s failed", fn.__name__)
            await asyncio.sleep(every)

    async def _run_jobs(self) -> None:
        await self.engine.run_due()

    async def _reconcile(self) -> None:
        _ = self.policy  # pick up policy changes committed by other nodes
        fleet = self.fleet()
        obs = Observations(
            services=fleet.services(),
            probes=self.probes,
            nodes=fleet.nodes(),
            findings=fleet.findings(),
            updates=fleet.updates(),
            publications={p.service_id: p.id for p in fleet.publications()},
            names={
                **{h.id: h.name for h in fleet.hosts()},
                **{s.id: s.name for s in fleet.services()},
                **{d.id: d.name for d in fleet.devices()},
            },
        )
        self.incidents = await self.reconciler.act(self.reconciler.propose(obs))
        self.publish({"type": "incidents"})

    async def _membership(self) -> None:
        """Mark silent nodes suspect or down and re-plan roles when membership changes."""
        now = datetime.now(UTC)
        nodes = self.fleet().nodes()
        changed = False
        for node in nodes:
            if node.id == self.config.node.id:
                node.last_seen, node.version = now, __version__
                node.capacity, node.usage = local_resources()
            age = now - node.last_seen
            state = NodeState(node.state)
            new = state
            if state not in (NodeState.DRAINING, NodeState.LEFT):
                new = (
                    NodeState.DOWN
                    if age > timedelta(seconds=90)
                    else NodeState.SUSPECT
                    if age > timedelta(seconds=30)
                    else NodeState.HEALTHY
                )
            if new != state or node.id == self.config.node.id:
                node.state = new
                changed = changed or new != state
                await self.write(
                    {
                        "op": "put",
                        "kind": "node",
                        "id": node.id,
                        "data": node.model_dump(mode="json"),
                    }
                )
        if not any(n.id == self.config.node.id for n in nodes) and not self.demo:
            me = Node(
                id=self.config.node.id,
                name=self.config.node.name,
                address=self.config.node.address,
                version=__version__,
                state=NodeState.HEALTHY,
                voter=True,
                site=self.config.node.site,
                labels=self.config.node.labels,
                last_seen=now,
                capacity=local_resources()[0],
                usage=local_resources()[1],
            )
            await self.write(
                {"op": "put", "kind": "node", "id": me.id, "data": me.model_dump(mode="json")}
            )
            changed = True
        if self.identity is not None:
            peers = json.loads(self.store.meta("peers") or "{}")
            port = self.config.listen.cluster.rpartition(":")[2]
            mine = f"https://{self.config.node.address}:{port}"
            if peers.get(self.config.node.id) != mine:
                peers[self.config.node.id] = mine
                await self.write({"op": "meta", "key": "peers", "value": json.dumps(peers)})
        current = json.loads(self.store.meta("placement") or "{}").get("assignments", {})
        if changed or not current:
            plan = placement.Planner().plan(self.fleet().nodes(), current)
            await self.write(
                {"op": "meta", "key": "placement", "value": json.dumps(plan.as_dict())}
            )
            current = plan.assignments
        await self._adjust_voters(current.get("controller", []))

    async def _adjust_voters(self, desired: list[str]) -> None:
        """Move the Raft voter set toward the planned controllers, one member per step."""
        if self.demo or not desired:
            return
        voters = set(self.raft.voters)
        want = set(desired)
        peers = json.loads(self.store.meta("peers") or "{}")
        add = sorted(n for n in want - voters if n in peers)
        remove = sorted(n for n in voters - want if n != self.config.node.id)
        if add:
            await self.raft.change_voters(sorted(voters | {add[0]}))
        elif remove and len(voters) > 1:
            await self.raft.change_voters(sorted(voters - {remove[0]}))
        else:
            return
        log.info("voters are now %s", ", ".join(self.raft.voters))
        for doc in self.store.list("node"):
            flag = doc["id"] in self.raft.voters
            if doc.get("voter") != flag:
                doc["voter"] = flag
                await self.write({"op": "put", "kind": "node", "id": doc["id"], "data": doc})

    async def _scan(self) -> None:
        """Security scans of every managed Linux host, prioritised by threat data and exposure."""
        from opstend.security import scanner
        from opstend.security.run import scan_host

        findings = []
        async with httpx.AsyncClient(timeout=60) as client:
            feeds = scanner.ThreatFeeds(client)
            try:
                await feeds.refresh_kev()
            except httpx.HTTPError as exc:
                log.warning("CISA KEV not reachable: %s", exc)
            for host in self.integrations.linux.values():
                try:
                    findings += (await scan_host(host, client)).findings
                except Exception as exc:
                    findings.append(scanner.coverage_gap(host.host_id, str(exc)))
            try:
                epss = await feeds.epss(f.cve for f in findings if f.cve)
            except httpx.HTTPError:
                epss = {}
        fleet = self.fleet()
        public = {f"service/{p.service_id}" for p in fleet.publications() if p.public}
        public |= {p.proxy_host for p in fleet.publications() if p.public}
        ranked = scanner.prioritize(findings, feeds.kev, epss, scanner.Exposure(public, set()))
        network = [i.finding() for i in fleet.insights]
        docs = {f.id: f.model_dump(mode="json") for f in [*ranked, *network]}
        await self.write({"op": "replace", "kind": "finding", "docs": docs})

    async def _discover(self) -> None:
        """Scan the configured private subnets for systems that are not managed yet."""
        found = await discovery.Prober().scan(self.config.discovery_subnets)
        known = {h.address.split("//")[-1].split(":")[0] for h in self.fleet().hosts()}
        known |= {d.address for d in self.fleet().devices() if d.address}
        current = {f"{c['kind']}:{c['address']}": c for c in self.store.list("candidate")}
        merged = discovery.merge(current, found, known)
        await self.write({"op": "replace", "kind": "candidate", "docs": merged})

    def _on_beacon(self, cand: discovery.Candidate) -> None:
        if self.raft.role == Role.LEADER:
            task = asyncio.ensure_future(
                self.write({"op": "put", "kind": "candidate", "id": cand.key, "data": asdict(cand)})
            )
            self._tasks.append(task)

    async def _collect(self) -> None:
        """Read every integration and replace the inventory kinds in one replicated write each."""
        snapshot = await self.integrations.collect()
        for kind, docs in snapshot.items():
            await self.write({"op": "replace", "kind": kind, "docs": docs})
        self.publish({"type": "tick"})

    async def _demo_tick(self) -> None:
        """Keep the demo alive: nodes report, usage moves, the sensor keeps failing."""
        now = datetime.now(UTC)
        for doc in self.store.list("node"):
            doc["last_seen"] = now.isoformat()
            self.store.put("node", doc["id"], doc)
        for doc in self.store.list("host"):
            if doc["usage"].get("cpu_pct") is not None:
                base = doc["usage"]["cpu_pct"]
                doc["usage"]["cpu_pct"] = round(min(98, max(2, base + random.uniform(-4, 4))), 1)  # noqa: S311
                self.store.put("host", doc["id"], doc)
        sensor = self.store.get("service", "sensorik")
        result = (
            ProbeResult.HEALTHY
            if sensor and sensor.get("health") == "ok"
            else ProbeResult.UNHEALTHY
        )
        self.probes.setdefault("sensorik", []).append(result)
        self.probes["sensorik"] = self.probes["sensorik"][-10:]
        self.publish({"type": "tick"})

    async def _seed_demo_jobs(self) -> None:
        from opstend.core.jobs import Step

        if self.engine.list():
            return
        fleet = self.fleet()
        for insight in fleet.insights:
            if insight.fix and insight.fix["action"] in self.registry.names():
                params = {k: v for k, v in insight.fix.items() if k not in ("summary", "action")}
                await self.engine.preview(
                    Job(
                        title=insight.fix["summary"],
                        category="network",
                        target=insight.objects[0],
                        reason=insight.title,
                        requested_by="zorec",
                        steps=[Step(action=insight.fix["action"], params=params)],
                    )
                )
                break
        await self.engine.preview(
            Job(
                title="Firmware 7.2.123 auf sw-werkstatt",
                category="upgrade",
                target="unifi:sw",
                reason="Neue Firmware behebt PoE-Abschaltungen unter Last",
                requested_by="zorec",
                steps=[Step(action="unifi.device_upgrade", params={"device": "unifi:sw"})],
            )
        )
        for _ in range(3):
            self.probes.setdefault("sensorik", []).append(ProbeResult.UNHEALTHY)
        await self._reconcile()
