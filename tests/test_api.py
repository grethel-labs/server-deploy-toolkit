import asyncio

import pytest
from fastapi.testclient import TestClient

from opstend.api.app import build_app
from opstend.config import Config
from opstend.runtime import Runtime


@pytest.fixture
def client():
    rt = Runtime(Config(), demo=True, demo_delay=0.0)
    with TestClient(build_app(rt)) as c:
        s = c.get("/api/session", headers={"accept-language": "en"}).json()
        c.headers["x-csrf-token"] = s["csrf"]
        c.rt = rt  # type: ignore[attr-defined]
        yield c


def test_session_and_security_headers(client):
    r = client.get("/api/session")
    assert r.json()["demo"] is True and r.json()["role"] == "admin"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_csrf_required_for_writes(client):
    token = client.headers.pop("x-csrf-token")
    r = client.post("/api/network/explain", json={"src": "a", "dst": "b"})
    assert r.status_code == 403
    client.headers["x-csrf-token"] = token


def test_overview_has_incidents_and_decisions(client):
    data = client.get("/api/overview").json()
    assert data["counts"]["hosts"] == 5
    assert data["decisions"], "demo seeds decisions"
    assert any(i["title"].startswith("Restart Sensorik") for i in data["incidents"])
    assert data["cluster"]["role"] == "leader"


def test_guest_job_preview_approve_run(client):
    guests = client.get("/api/guests").json()
    fotos = next(g for g in guests if g["name"] == "fotos")
    assert "snapshot" in fotos["capabilities"]
    job = client.post(
        f"/api/guests/{fotos['id']}/jobs",
        json={"action": "power", "params": {"action": "shutdown"}},
    ).json()
    assert job["state"] == "awaiting_approval" and "fotos" in job["title"]
    client.post(f"/api/jobs/{job['id']}/approve", json={"fingerprint": job["fingerprint"]})
    for _ in range(50):
        state = client.get(f"/api/jobs/{job['id']}").json()["job"]["state"]
        if state == "succeeded":
            break
        asyncio.run(asyncio.sleep(0.02))
    assert state == "succeeded"
    assert client.get(f"/api/guests/{fotos['id']}").json()["guest"]["state"] == "stopped"


def test_network_topology_insights_and_explain(client):
    net = client.get("/api/network").json()
    codes = {i["code"] for i in net["insights"]}
    assert {"vlan-not-carried", "link-downgraded", "poe-budget"} & codes
    topo = client.get("/api/network/topology?layers=physical,network").json()
    assert topo["source"].startswith("@startuml") and "unifi_sw" in topo["hooks"]
    path = client.post(
        "/api/network/explain", json={"src": "client:a4:83:e7:00:00:12", "dst": "pve:home:101"}
    ).json()
    assert path["reachable"] is False  # guest network is isolated


def test_updates_plan_and_rollout(client):
    data = client.get("/api/updates").json()
    assert data["waves"] and data["plan_error"] is None
    job = client.post("/api/rollouts", json={}).json()
    assert job["state"] == "awaiting_approval" and len(job["steps"]) >= 3


def test_zorec_rules_answer(client):
    a = client.post(
        "/api/zorec", json={"question": "Welche Updates gibt es?", "mode": "edit"}
    ).json()
    assert a["source"] == "rules" and a["actions"][0]["action"] == "rollout.plan"


def test_deploy_analyze_and_validate(client):
    r = client.post(
        "/api/deploy/analyze", json={"repo": "https://github.com/werkstatt/wetterstation"}
    ).json()
    names = [s["name"] for s in r["spec"]["services"]]
    assert names == ["web", "db"]
    db = r["spec"]["services"][1]
    assert any(e["name"] == "POSTGRES_PASSWORD" and e["secret"] for e in db["env"])
    job = client.post("/api/deploy", json={"spec": r["spec"], "host": r["suggested_host"]}).json()
    assert job["state"] == "awaiting_approval"


def test_policy_requires_admin_and_roundtrips(client):
    p = client.get("/api/policy").json()
    p["kill_switch"] = True
    assert client.put("/api/policy", json=p).json()["kill_switch"] is True
