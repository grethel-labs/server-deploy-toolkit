"""Three real OpsTend processes form a cluster, survive the loss of the leader.

Runs the actual `opstend serve` command with TLS, join tokens and mutual TLS
between the nodes. Takes about a minute; run with `pytest -m cluster`.
"""

import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.cluster


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tls(dir: Path) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (dir / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (dir / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(dir / "c.pem"), str(dir / "k.pem")


class Node:
    def __init__(self, root: Path, name: str, token: str | None = None):
        self.dir = root / name
        self.dir.mkdir()
        self.web, self.cluster_port = free_port(), free_port()
        cert, key = tls(self.dir)
        token_file = self.dir / "join-token"
        if token:
            token_file.write_text(token)
        (self.dir / "opstend.yaml").write_text(
            json.dumps(
                {
                    "node": {"id": f"node-{name}", "name": name, "address": "127.0.0.1"},
                    "data_dir": str(self.dir / "data"),
                    "listen": {
                        "web": f"127.0.0.1:{self.web}",
                        "cluster": f"127.0.0.1:{self.cluster_port}",
                    },
                    "cluster": {"join_token_file": str(token_file)},
                }
            )
        )
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "faulthandler",
                "-m",
                "opstend",
                "serve",
                "--config",
                str(self.dir / "opstend.yaml"),
                "--tls-cert",
                cert,
                "--tls-key",
                key,
            ],
            stdout=open(self.dir / "log", "w"),
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        self.client = httpx.Client(
            base_url=f"https://127.0.0.1:{self.web}", verify=False, timeout=10
        )

    def wait(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            try:
                return self.client.get("/api/session").json()
            except httpx.HTTPError:
                time.sleep(0.3)
        raise AssertionError((self.dir / "log").read_text()[-3000:])

    def login(self):
        s = self.client.post(
            "/api/setup", json={"username": "admin", "password": "correct horse battery"}
        )
        if s.status_code == 409:
            s = self.client.post(
                "/api/session", json={"username": "admin", "password": "correct horse battery"}
            )
        self.client.headers["x-csrf-token"] = s.json()["csrf"]

    def cluster(self):
        return self.client.get("/api/cluster").json()

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError(f"{self.dir.name} did not stop on SIGTERM") from None


def until(fn, timeout=90, step=1.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:
            last = exc
        time.sleep(step)
    raise AssertionError(f"condition not met: {last}")


def test_three_nodes_join_and_survive_leader_loss(tmp_path):
    a = Node(tmp_path, "a")
    nodes = [a]
    try:
        a.wait()
        a.login()
        until(lambda: a.cluster()["raft"]["role"] == "leader", 20)
        token = a.client.post("/api/cluster/tokens").json()["token"]
        b = Node(tmp_path, "b", token)
        nodes.append(b)
        token = a.client.post("/api/cluster/tokens").json()["token"]
        c = Node(tmp_path, "c", token)
        nodes.append(c)
        b.wait()
        c.wait()
        voters = until(
            lambda: len(a.cluster()["raft"]["voters"]) == 3 and a.cluster()["raft"]["voters"]
        )
        assert sorted(voters) == ["node-a", "node-b", "node-c"]
        # Writes on the leader reach the followers.
        policy = a.client.get("/api/policy").json()
        policy["kill_switch"] = True
        assert a.client.put("/api/policy", json=policy).status_code == 200
        b.login()
        until(lambda: b.client.get("/api/policy").json()["kill_switch"] is True, 20)
        # The leader fails; the other two elect a new one and keep accepting writes.
        a.stop()
        nodes.remove(a)
        leader = until(
            lambda: next((n for n in (b, c) if n.cluster()["raft"]["role"] == "leader"), None), 30
        )
        leader.login()
        policy["kill_switch"] = False
        # Right after an election the first write may wait for the new term to commit.
        until(lambda: leader.client.put("/api/policy", json=policy).status_code == 200, 30)
    finally:
        for n in nodes:
            n.stop()
