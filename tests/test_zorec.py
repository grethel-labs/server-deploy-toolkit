import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from opstend.api.app import build_app
from opstend.config import Config
from opstend.core.audit import AuditLog
from opstend.core.jobs import ActionRegistry, Engine
from opstend.core.store import Store
from opstend.demo import fixtures
from opstend.runtime import Runtime
from opstend.state import Fleet
from opstend.zorec import selection
from opstend.zorec.assistant import Zorec
from opstend.zorec.providers import Kind, OpenAICompatible, ProviderConfig, Reply
from opstend.zorec.settings import AISettings, Capability, Prefer


def cfg(**kw):
    base = {"id": "lab", "label": "Lab", "kind": Kind.OPENAI, "url": "http://m/v1", "local": True}
    return ProviderConfig(**(base | kw))


def solve(prompt: str) -> str:
    """What a capable model answers to the suite."""
    if "max_severity" in prompt:
        levels = re.findall(r"severity=(\w+)", prompt)
        order = ["low", "medium", "high", "critical"]
        top = max(levels, key=order.index)
        return json.dumps({"max_severity": top, "count": len(levels)})
    if '"instructions"' in prompt:
        data = prompt.split("<data>")[1].split("</data>")[0].strip().splitlines()
        bad = sum(1 for line in data if "IGNORE" in line or "SYSTEM:" in line)
        disk = int(re.search(r"disk (\d+) percent", prompt).group(1))
        return json.dumps({"disk_percent": disk, "instructions": bad})
    if "Allowed actions" in prompt:
        return (
            'Start grafana.\n```actions\n[{"title": "Start grafana", "action": "guest.power",'
            ' "params": {"guest": "pve:node2:105", "action": "start"}}]\n```'
        )
    if "flag=amber" in prompt:
        return re.search(r"(host-\d{4}) site=lab cpu=\d+% flag=amber", prompt).group(1)
    return "OK"


class Fake:
    def __init__(self, config, models, weak=(), seconds=None):
        self.config = config
        self._models = models
        self.weak = set(weak)
        self.seconds = seconds or {}
        self.calls = []

    @property
    def model(self):
        return self.config.model or ""

    async def models(self):
        return self._models

    async def complete(self, system, messages, max_tokens=None):
        self.calls.append(messages)
        prompt = messages[-1]["content"]
        text = "PWNED" if self.model in self.weak else solve(prompt)
        return Reply(text, self.model, seconds=self.seconds.get(self.model, 1.0))


def test_permissions_follow_their_requirements():
    ai = AISettings(enabled=True, providers=[cfg(local=False)], active="lab")
    ai.capabilities[Capability.PROPOSE] = False
    ai.capabilities[Capability.RUN] = True
    assert not ai.allowed(Capability.RUN), "acting alone needs preparing changes"
    assert ai.usable("lab") is None, "an external provider needs the external permission"
    ai.capabilities[Capability.EXTERNAL] = True
    assert ai.usable("lab") is not None
    ai.enabled = False
    assert ai.usable("lab") is None


async def test_model_search_picks_the_best_passing_model_and_keeps_a_fallback():
    c = cfg(model="tiny-e2b")
    ai = AISettings(enabled=True, providers=[c], active="lab")
    fake = Fake(c, ["tiny-e2b", "big-26b", "nomic-embed"], weak={"tiny-e2b"})
    decision = await selection.search(ai, {"lab": fake})
    results = {r["model"]: r for r in decision["results"]}
    assert "nomic-embed" not in results, "embedding models are excluded"
    assert results["big-26b"]["ok"] and not results["tiny-e2b"]["ok"]
    assert decision["action"] == "switch" and decision["best"]["model"] == "big-26b"
    updated = selection.apply(ai, decision)
    assert updated.provider("lab").model == "big-26b"
    assert updated.provider(updated.fallback).model == "tiny-e2b"


async def test_model_search_keeps_the_incumbent_when_nothing_is_better():
    c = cfg(model="big-26b")
    ai = AISettings(enabled=True, providers=[c], active="lab")
    ai.selection.prefer = Prefer.QUALITY
    fake = Fake(c, ["big-26b", "small-7b"])
    decision = await selection.search(ai, {"lab": fake})
    assert decision["action"] == "kept"
    assert selection.apply(ai, decision) == ai


def test_size_hints_rank_models():
    assert selection.size_hint("gemma4:26b-a4b-it-q4_K_M") == 26
    assert selection.size_hint("gemma4:e2b-it-q4_K_M") == 2
    assert selection.size_hint("mixtral:8x7b") == 56
    assert selection.size_hint("claude-opus-5-5") > selection.size_hint("claude-haiku-5-5")


@pytest.fixture
def zorec_parts():
    store = Store()
    fixtures.load(store)
    return Fleet(store), Engine(store, AuditLog(), ActionRegistry())


async def test_small_context_windows_get_a_bounded_snapshot(zorec_parts):
    fleet, engine = zorec_parts
    c = cfg(model="gemma", context_tokens=2048, max_reply_tokens=400)
    fake = Fake(c, ["gemma"])
    z = Zorec(fleet, engine, fake, ["guest.power"])
    await z.ask("Erkläre mir bitte kurz den Zustand meines Clusters", history=[])
    sent = fake.calls[-1][-1]["content"]
    assert len(sent) < 2048 * 3, "the snapshot fits the window"
    assert "## hosts" in sent or "## problems" in sent


async def test_ui_actions_are_parsed_and_limited_by_permission(zorec_parts):
    fleet, engine = zorec_parts
    c = cfg(model="m")

    class Guide(Fake):
        async def complete(self, system, messages, max_tokens=None):
            return Reply(
                'Look here.\n```ui\n[{"kind": "navigate", "target": "#/network"},'
                ' {"kind": "navigate", "target": "https://evil"},'
                ' {"kind": "highlight", "target": "insight-1", "label": "VLAN"}]\n```',
                "m",
            )

    z = Zorec(fleet, engine, Guide(c, ["m"]), [])
    a = await z.ask(
        "Warum ist das WLAN langsam heute Abend?", history=[{"role": "user", "content": "x"}]
    )
    assert a.text == "Look here."
    assert [u.kind for u in a.ui] == ["navigate", "highlight"], "external targets are dropped"
    z.capabilities.discard(Capability.GUIDE.value)
    a = await z.ask(
        "Warum ist das WLAN langsam heute Abend?", history=[{"role": "user", "content": "x"}]
    )
    assert a.ui == []


async def test_rules_navigate_without_a_model(zorec_parts):
    fleet, engine = zorec_parts
    a = await Zorec(fleet, engine).ask("Öffne die Topologie")
    assert a.source == "rules" and a.ui[0].target == "#/network/topology"


async def test_fallback_provider_answers_when_the_first_fails(zorec_parts):
    from opstend.zorec.providers import ProviderError

    fleet, engine = zorec_parts

    class Down(Fake):
        async def complete(self, *a, **k):
            raise ProviderError("down")

    z = Zorec(
        fleet,
        engine,
        Down(cfg(id="a", model="x"), []),
        [],
        fallback=Fake(cfg(id="b", label="Backup", model="y"), ["y"]),
    )
    a = await z.ask(
        "Erzähl mir etwas über meine Systeme", history=[{"role": "user", "content": "x"}]
    )
    assert a.source == "model" and a.provider == "Backup"


async def test_openai_compatible_provider_lists_and_completes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    p = OpenAICompatible(cfg(model="a"), "k", httpx.MockTransport(handler))
    assert await p.models() == ["a", "b"]
    r = await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert r.text == "OK" and r.tokens_in == 5


@pytest.fixture
def client():
    rt = Runtime(Config(), demo=True, demo_delay=0.0)
    with TestClient(build_app(rt)) as c:
        s = c.get("/api/session", headers={"accept-language": "en"}).json()
        c.headers["x-csrf-token"] = s["csrf"]
        c.rt = rt
        yield c


def test_settings_store_keys_in_the_vault_and_never_return_them(client):
    data = client.get("/api/zorec/settings").json()
    assert data["status"]["mode"] == "rules", "AI is off in the demo"
    assert {c["id"] for c in data["catalog"]["capabilities"]} >= {"answer", "run", "console"}
    settings = data["settings"]
    settings["enabled"] = True
    settings["providers"].append(
        {"id": "claude", "label": "Claude", "kind": "anthropic", "model": "claude-opus-5-5"}
    )
    r = client.put(
        "/api/zorec/settings", json={"settings": settings, "api_keys": {"claude": "sk-test-123"}}
    )
    assert r.status_code == 200, r.text
    assert "sk-test-123" not in r.text
    stored = next(p for p in r.json()["settings"]["providers"] if p["id"] == "claude")
    assert stored["has_key"] is True and "secret_id" not in stored
    assert client.rt.vault.reveal("zorec-claude") == "sk-test-123"
    assert client.get("/api/zorec/status").json()["mode"] == "model"
    # Saving again without a key keeps the stored one.
    again = r.json()["settings"]
    client.put("/api/zorec/settings", json={"settings": again})
    assert client.rt.ai.provider("claude").secret_id == "zorec-claude"


def test_full_automation_preset(client):
    policy = client.post("/api/policy/preset", json={"preset": "full"}).json()
    assert set(policy["autonomy"].values()) == {"auto"}
    assert client.rt.ai.capabilities["run"] is True
    policy = client.post("/api/policy/preset", json={"preset": "careful"}).json()
    assert policy["autonomy"]["patch"] == "propose"
    assert client.rt.ai.capabilities["run"] is False


def test_zorec_works_without_ai(client):
    r = client.post("/api/zorec", json={"question": "Zeige mir die Sicherheit"}).json()
    assert r["source"] == "rules" and r["ui"][0]["target"] == "#/security"
