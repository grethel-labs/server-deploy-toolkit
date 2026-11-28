"""API for the application catalog, workloads, sandboxes and adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from opstend.core.jobs import Job, Step
from opstend.core.models import Host
from opstend.deploy import analyzer
from opstend.i18n import tr
from opstend.workloads import manager
from opstend.workloads.catalog import AdapterStatus, adapter_from_spec
from opstend.workloads.models import (
    Connection,
    Environment,
    NetworkPolicy,
    Workload,
    WorkloadState,
    promote,
    sandbox_from,
)
from opstend.workloads.plan import isolation_summary

if TYPE_CHECKING:
    from opstend.runtime import Runtime


class WorkloadAction(BaseModel):
    action: str
    purge: bool = False
    hours: int = Field(default=4, ge=1, le=72)


class NetworkChange(BaseModel):
    network: NetworkPolicy
    connections: list[Connection] = Field(default_factory=list)


class DraftAdapter(BaseModel):
    repo: str
    name: str | None = None


def routes(
    app: FastAPI,
    rt: Runtime,
    viewer: Callable[..., Any],
    operator: Callable[..., Any],
    admin: Callable[..., Any],
    dump: Callable[[Any], Any],
    fetch_repo: Callable[[str], Any],
) -> None:
    def workloads() -> list[Workload]:
        return [Workload.model_validate(d) for d in rt.store.list("workload")]

    def hosts() -> list[Host]:
        return rt.fleet().hosts()

    def get(workload_id: str) -> Workload:
        doc = rt.store.get("workload", workload_id)
        if doc is None:
            raise HTTPException(404, tr("Workload not found"))
        return Workload.model_validate(doc)

    async def job(title: str, target: str, steps: list[Step], user: str, reason: str = "") -> Any:
        created = await rt.engine.preview(
            Job(
                title=title,
                category="deploy",
                target=target,
                steps=steps,
                requested_by=user,
                reason=reason,
            )
        )
        if not title:
            created.title = created.steps[0].effect
            await rt.engine._save(created)
        return dump(created)

    # -- catalog -------------------------------------------------------------

    @app.get("/api/catalog")
    async def catalog(_: Any = Depends(viewer)) -> list[dict[str, Any]]:
        installed = {w.adapter for w in workloads() if w.state != WorkloadState.REMOVED}
        return [
            {**a.model_dump(mode="json"), "installed": a.id in installed}
            for a in sorted(rt.adapters().values(), key=lambda a: (a.status != "curated", a.name))
        ]

    @app.get("/api/catalog/{adapter_id}")
    async def adapter(adapter_id: str, _: Any = Depends(viewer)) -> dict[str, Any]:
        found = rt.adapters().get(adapter_id)
        if found is None:
            raise HTTPException(404, tr("Adapter not found"))
        data: dict[str, Any] = found.model_dump(mode="json")
        return data

    @app.post("/api/catalog/draft")
    async def draft_adapter(body: DraftAdapter, s: Any = Depends(operator)) -> dict[str, Any]:
        """Zorec reads a repository and drafts an adapter; it must pass a sandbox before use."""
        files, revision = await fetch_repo(body.repo)
        name = body.name or body.repo.rstrip("/").rsplit("/", 1)[-1]
        spec = analyzer.analyze(files, name, body.repo, revision)
        if not spec.services:
            raise HTTPException(422, tr("No container definition found in this repository."))
        draft = adapter_from_spec(spec, body.repo)
        await rt.write(
            {"op": "put", "kind": "adapter", "id": draft.id, "data": draft.model_dump(mode="json")}
        )
        rt.audit.record(s.user, "adapter.draft", f"adapter/{draft.id}", detail={"repo": body.repo})
        return {"adapter": draft.model_dump(mode="json"), "questions": spec.questions}

    @app.post("/api/catalog/{adapter_id}/approve")
    async def approve_adapter(adapter_id: str, s: Any = Depends(admin)) -> dict[str, Any]:
        doc = rt.store.get("adapter", adapter_id)
        if doc is None:
            raise HTTPException(404, tr("Only drafted adapters need approval"))
        if doc["status"] != AdapterStatus.TESTED:
            raise HTTPException(409, tr("Test the adapter in a sandbox first."))
        doc["status"] = AdapterStatus.APPROVED
        await rt.write({"op": "put", "kind": "adapter", "id": adapter_id, "data": doc})
        rt.audit.record(s.user, "adapter.approve", f"adapter/{adapter_id}")
        return dict(doc)

    # -- workloads -----------------------------------------------------------

    @app.get("/api/workloads")
    async def list_workloads(_: Any = Depends(viewer)) -> list[dict[str, Any]]:
        names = {h.id: h.name for h in hosts()}
        return [
            {**dump(w), "host_name": names.get(w.host_id or ""), "isolation": isolation_summary(w)}
            for w in workloads()
            if w.state != WorkloadState.REMOVED
        ]

    @app.get("/api/workloads/{workload_id}")
    async def show(workload_id: str, _: Any = Depends(viewer)) -> dict[str, Any]:
        w = get(workload_id)
        guests = [g for g in rt.fleet().guests() if f"opstend.workload={w.id}" in g.tags]
        return {
            "workload": dump(w),
            "isolation": isolation_summary(w),
            "guests": dump(guests),
            "jobs": dump([j for j in rt.engine.list() if j.target == f"workload/{w.id}"][:10]),
            "adapter": dump(rt.adapters().get(w.adapter or "")),
            "host": dump(next((h for h in hosts() if h.id == w.host_id), None)),
        }

    def resolve(req: manager.InstallRequest, secret_ids: dict[str, str]) -> tuple[Workload, Any]:
        found = rt.adapters().get(req.adapter)
        if found is None:
            raise HTTPException(404, tr("Adapter not found"))
        if found.status == AdapterStatus.DRAFT and req.environment != Environment.SANDBOX:
            raise HTTPException(
                409, tr("A drafted adapter runs only in a sandbox until it passes.")
            )
        try:
            built = manager.build(req, found, secret_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        existing = rt.store.get("workload", built.id)
        if existing and existing.get("state") != WorkloadState.REMOVED:
            raise HTTPException(409, tr("The name {id} is already used.", id=built.id))
        return built, found

    @app.post("/api/workloads/preview")
    async def preview(req: manager.InstallRequest, _: Any = Depends(operator)) -> dict[str, Any]:
        found = rt.adapters().get(req.adapter)
        if found is None:
            raise HTTPException(404, tr("Adapter not found"))
        w, adapter_def = resolve(req, manager.secret_placeholders(found))
        result: dict[str, Any] = manager.preview(w, adapter_def, hosts(), workloads())
        return result

    @app.post("/api/workloads")
    async def install(req: manager.InstallRequest, s: Any = Depends(operator)) -> Any:
        found = rt.adapters().get(req.adapter)
        if found is None:
            raise HTTPException(404, tr("Adapter not found"))
        secret_ids = {name: manager.new_secret_id(req.id, name) for name in req.secrets}
        w, adapter_def = resolve(req, secret_ids)
        info = manager.preview(w, adapter_def, hosts(), workloads())
        if info["host"] is None:
            raise HTTPException(409, info["problems"][0])
        w.host_id = info["host"]
        w.created_by = s.user
        for name, value in req.secrets.items():
            doc = rt.vault.seal(
                secret_ids[name], value, label=f"{w.name}: {name}", owner=f"workload/{w.id}"
            )
            await rt.write({"op": "put", "kind": "secret", "id": secret_ids[name], "data": doc})
        await rt.write(
            {"op": "put", "kind": "workload", "id": w.id, "data": w.model_dump(mode="json")}
        )
        rt.audit.record(
            s.user,
            "workload.create",
            f"workload/{w.id}",
            detail={"adapter": w.adapter, "environment": w.environment},
        )
        title = (
            tr("Test {name} in a sandbox", name=w.name)
            if w.environment == Environment.SANDBOX
            else tr("Install {name}", name=w.name)
        )
        return await job(
            title,
            f"workload/{w.id}",
            [Step(action="workload.deploy", params={"workload": w.id, "host": w.host_id})],
            s.user,
            reason=", ".join(x["text"] for x in info["isolation"][:3]),
        )

    @app.post("/api/workloads/{workload_id}/actions")
    async def act(workload_id: str, body: WorkloadAction, s: Any = Depends(operator)) -> Any:
        w = get(workload_id)
        target = f"workload/{w.id}"
        if body.action in ("start", "stop", "restart"):
            return await job(
                "",
                target,
                [Step(action="workload.power", params={"workload": w.id, "action": body.action})],
                s.user,
            )
        if body.action == "test":
            return await job(
                "", target, [Step(action="workload.test", params={"workload": w.id})], s.user
            )
        if body.action == "remove":
            return await job(
                "",
                target,
                [Step(action="workload.remove", params={"workload": w.id, "purge": body.purge})],
                s.user,
            )
        if body.action == "promote":
            if w.environment != Environment.SANDBOX:
                raise HTTPException(409, tr("Only sandboxes can be taken into operation."))
            if any(not t.get("ok") for t in w.tests):
                raise HTTPException(409, tr("All checks must pass first."))
            prod = promote(w)
            original = rt.store.get("workload", prod.id)
            if original and original.get("state") not in ("removed", "draft"):
                raise HTTPException(409, tr("{id} is already in operation.", id=prod.id))
            if original:
                prod.network = Workload.model_validate(original).network
                prod.connections = Workload.model_validate(original).connections
            prod.host_id = w.host_id
            await rt.write(
                {
                    "op": "put",
                    "kind": "workload",
                    "id": prod.id,
                    "data": prod.model_dump(mode="json"),
                }
            )
            if w.adapter and (a := rt.store.get("adapter", w.adapter)) and a["status"] == "draft":
                a["status"] = AdapterStatus.TESTED
                await rt.write({"op": "put", "kind": "adapter", "id": a["id"], "data": a})
            return await job(
                tr("Take {name} into operation", name=prod.name),
                f"workload/{prod.id}",
                [
                    Step(action="workload.deploy", params={"workload": prod.id, "host": w.host_id}),
                    Step(action="workload.remove", params={"workload": w.id, "purge": True}),
                ],
                s.user,
                reason=tr("All checks passed in the sandbox."),
            )
        if body.action == "sandbox":
            sbx = sandbox_from(w, body.hours)
            sbx.host_id = w.host_id
            await rt.write(
                {"op": "put", "kind": "workload", "id": sbx.id, "data": sbx.model_dump(mode="json")}
            )
            return await job(
                tr("Test a copy of {name} in a sandbox", name=w.name),
                f"workload/{sbx.id}",
                [Step(action="workload.deploy", params={"workload": sbx.id, "host": w.host_id})],
                s.user,
            )
        raise HTTPException(422, tr("Unknown action"))

    @app.put("/api/workloads/{workload_id}/network")
    async def change_network(
        workload_id: str, body: NetworkChange, s: Any = Depends(operator)
    ) -> Any:
        w = get(workload_id)
        peers = {x.id for x in workloads()}
        for c in body.connections:
            if c.to_workload not in peers or c.to_workload == w.id:
                raise HTTPException(422, tr("Unknown target workload {id}", id=c.to_workload))
        w.network, w.connections = body.network, body.connections
        await rt.write(
            {"op": "put", "kind": "workload", "id": w.id, "data": w.model_dump(mode="json")}
        )
        return await job(
            tr("Apply the network rules of {name}", name=w.name),
            f"workload/{w.id}",
            [
                Step(action="workload.remove", params={"workload": w.id, "purge": False}),
                Step(action="workload.deploy", params={"workload": w.id, "host": w.host_id}),
            ],
            s.user,
            reason=", ".join(x["text"] for x in isolation_summary(w)),
        )
