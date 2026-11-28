"""API for Zorec's AI settings, permissions, providers and the model search."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from opstend.core.policy import PRESETS as POLICY_PRESETS
from opstend.core.policy import preset as policy_preset
from opstend.i18n import tr
from opstend.zorec.providers import PRESETS, ProviderConfig, ProviderError, build
from opstend.zorec.settings import AISettings, Capability, catalog, status

if TYPE_CHECKING:
    from opstend.runtime import Runtime


class SettingsChange(BaseModel):
    settings: dict[str, Any]  # AISettings as returned by GET (with has_key flags)
    api_keys: dict[str, str] = Field(default_factory=dict)  # provider id -> key (write-only)


class ProviderTest(BaseModel):
    provider: ProviderConfig
    api_key: str | None = None


class PresetChoice(BaseModel):
    preset: str


def public(ai: AISettings) -> dict[str, Any]:
    data = ai.model_dump(mode="json")
    for p in data["providers"]:
        p["has_key"] = bool(p.pop("secret_id", None))
    return data


def routes(
    app: FastAPI,
    rt: Runtime,
    viewer: Callable[..., Any],
    admin: Callable[..., Any],
    dump: Callable[[Any], Any],
) -> None:
    @app.get("/api/zorec/status")
    async def zorec_status(_: Any = Depends(viewer)) -> dict[str, Any]:
        ai = rt.ai
        return {
            **dump(status(ai)),
            "enabled": ai.enabled,
            "capabilities": sorted(c.value for c in Capability if ai.allowed(c)),
        }

    @app.get("/api/zorec/settings")
    async def get_settings(_: Any = Depends(admin)) -> dict[str, Any]:
        history = json.loads(rt.store.meta("zorec.selection") or "[]")
        return {
            "settings": public(rt.ai),
            "status": dump(status(rt.ai)),
            "catalog": dump(catalog()),
            "presets": [
                {**asdict(p), "label": tr(p.label), "note": tr(p.note) if p.note else ""}
                for p in PRESETS.values()
            ],
            "selection": {"running": rt._selection_running, "history": history[-5:]},
            "policy_presets": list(POLICY_PRESETS),
        }

    @app.put("/api/zorec/settings")
    async def put_settings(body: SettingsChange, s: Any = Depends(admin)) -> dict[str, Any]:
        raw = dict(body.settings)
        raw["providers"] = [
            {k: v for k, v in p.items() if k not in ("has_key", "secret_id")}
            for p in raw.get("providers", [])
        ]
        try:
            new = AISettings.model_validate(raw)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        ids = [p.id for p in new.providers]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, tr("Every provider needs its own id."))
        for ref in (new.active, new.fallback):
            if ref and ref not in ids:
                raise HTTPException(422, tr("Unknown provider {id}", id=ref))
        old = rt.ai
        stored = {p.id: p.secret_id for p in old.providers}
        for p in new.providers:
            # The client never sees secret ids; keep the stored one unless a new key arrives.
            p.secret_id = stored.get(p.id)
            key = body.api_keys.get(p.id)
            if key:
                p.secret_id = f"zorec-{p.id}"
                doc = rt.vault.seal(
                    p.secret_id, key, label=f"Zorec: {p.label}", owner=f"zorec/{p.id}"
                )
                await rt.write({"op": "put", "kind": "secret", "id": p.secret_id, "data": doc})
        for gone in set(stored) - set(ids):
            if stored[gone]:
                await rt.write({"op": "delete", "kind": "secret", "id": stored[gone]})
        await rt.save_ai(new)
        before = {c.value for c in Capability if old.allowed(c)}
        after = {c.value for c in Capability if new.allowed(c)}
        rt.audit.record(
            s.user,
            "zorec.settings",
            "zorec",
            detail={
                "enabled": new.enabled,
                "active": new.active,
                "granted": sorted(after - before),
                "revoked": sorted(before - after),
            },
        )
        rt.publish({"type": "zorec.settings"})
        return {"settings": public(rt.ai), "status": dump(status(rt.ai))}

    @app.post("/api/zorec/providers/test")
    async def test_provider(body: ProviderTest, _: Any = Depends(admin)) -> dict[str, Any]:
        cfg = body.provider
        key = body.api_key
        if not key:
            stored = rt.ai.provider(cfg.id)
            if stored and stored.secret_id:
                key = rt.vault.reveal(stored.secret_id)
        try:
            provider = build(cfg, key)
        except ProviderError as exc:
            return {"ok": False, "error": str(exc), "models": []}
        models: list[str] = []
        error = None
        try:
            models = await provider.models()
        except ProviderError as exc:
            error = str(exc)
        if not cfg.model and models:
            cfg.model = models[0]
        reply = None
        if cfg.model:
            try:
                r = await provider.complete(
                    "Reply with exactly: OK", [{"role": "user", "content": "Ping"}], 16
                )
                reply = {"text": r.text[:80], "model": r.model, "seconds": round(r.seconds, 1)}
            except ProviderError as exc:
                error = str(exc)
        return {"ok": reply is not None, "error": error, "models": models, "reply": reply}

    @app.post("/api/zorec/selection", status_code=202)
    async def start_selection(s: Any = Depends(admin)) -> dict[str, Any]:
        if rt._selection_running:
            raise HTTPException(409, tr("A model search is already running."))
        if not rt.ai.enabled:
            raise HTTPException(409, tr("Turn on AI first."))

        async def run() -> None:
            try:
                await rt.run_selection(s.user)
            except Exception as exc:  # reported through the history and the log
                rt.publish({"type": "zorec.selection", "error": str(exc)})
            rt.publish({"type": "zorec.selection", "done": True})

        rt._tasks.append(asyncio.create_task(run()))
        return {"running": True}

    @app.get("/api/zorec/selection")
    async def get_selection(_: Any = Depends(admin)) -> dict[str, Any]:
        history = json.loads(rt.store.meta("zorec.selection") or "[]")
        return {"running": rt._selection_running, "history": history[-5:]}

    @app.post("/api/policy/preset")
    async def apply_preset(body: PresetChoice, s: Any = Depends(admin)) -> Any:
        if body.preset not in POLICY_PRESETS:
            raise HTTPException(422, tr("Unknown preset"))
        policy = policy_preset(body.preset, rt.policy)
        await rt.save_policy(policy)
        ai = rt.ai.model_copy(deep=True)
        ai.capabilities[Capability.RUN] = body.preset == "full"
        await rt.save_ai(ai)
        rt.audit.record(s.user, "policy.preset", "policy", detail={"preset": body.preset})
        return dump(policy)
