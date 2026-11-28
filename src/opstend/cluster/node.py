"""This node's cluster identity and the cluster endpoints.

* The first node creates the cluster CA and issues itself a certificate.
* Another node joins with a token: it checks the CA fingerprint in the token,
  sends a certificate request with an HMAC proof to the issuing node's web
  port, and receives its certificate, the CA and the peer list.
* Nodes talk Raft, forward writes to the leader and send heartbeats over the
  cluster port with mutual TLS. Only certificates from the cluster CA are
  accepted, and the certificate's node id must match the claimed sender.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from opstend.cluster.consensus import NotLeader, SnapshotRequest, VoteRequest
from opstend.cluster.enrollment import ClusterCA, EnrollmentError, key_pem, make_csr, new_key
from opstend.cluster.transport import decode_append

if TYPE_CHECKING:
    from opstend.runtime import Runtime

log = logging.getLogger("opstend.cluster")


class Identity:
    """Files under <data_dir>/cluster: ca.crt, (ca.key on voters), node.crt, node.key."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "cluster"

    @property
    def exists(self) -> bool:
        return (self.dir / "node.crt").exists()

    def path(self, name: str) -> Path:
        return self.dir / name

    def bootstrap(self, cluster_name: str, node_id: str, addresses: list[str]) -> ClusterCA:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ca = ClusterCA.create(cluster_name)
        key = new_key()
        cert = ca.sign(make_csr(key, node_id, addresses))
        self._write("ca.crt", ca.cert_pem)
        self._write("ca.key", ca.key_pem(), secret=True)
        self._write("node.key", key_pem(key), secret=True)
        self._write("node.crt", cert)
        return ca

    def ca(self) -> ClusterCA | None:
        if not self.path("ca.key").exists():
            return None
        return ClusterCA(self.path("ca.crt").read_bytes(), self.path("ca.key").read_bytes())

    def _write(self, name: str, data: bytes, secret: bool = False) -> None:
        p = self.path(name)
        p.write_bytes(data)
        p.chmod(0o600 if secret else 0o644)

    def server_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(self.path("ca.crt")))
        ctx.load_cert_chain(str(self.path("node.crt")), str(self.path("node.key")))
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        return ctx

    def client_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=str(self.path("ca.crt")))
        ctx.load_cert_chain(str(self.path("node.crt")), str(self.path("node.key")))
        ctx.check_hostname = False
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        return ctx

    async def join(self, token: str, node_id: str, info: dict[str, Any]) -> dict[str, Any]:
        """Join the cluster that issued `token`. Returns peers and voters."""
        fp, secret = ClusterCA.parse_token(token)
        server = ClusterCA.token_address(token)
        if not server:
            raise EnrollmentError("the token carries no address; create a new token in the web app")
        async with httpx.AsyncClient(verify=False, timeout=15) as client:  # noqa: S501
            # Trust on first use, pinned by the fingerprint inside the token.
            ca_pem = (await client.get(f"https://{server}/api/cluster/ca")).content
            ca_fp = _fingerprint(ca_pem)
            if not ca_fp.startswith(fp):
                raise EnrollmentError("the cluster CA does not match the token")
        key = new_key()
        csr = make_csr(key, node_id, info.get("addresses", []))
        # The web port may use any certificate. The exchange stays safe without trusting it:
        # the HMAC proof binds the token to this certificate request, and the answer is only
        # accepted if the pinned cluster CA signed a certificate for exactly our key.
        async with httpx.AsyncClient(verify=False, timeout=15) as client:  # noqa: S501
            resp = await client.post(
                f"https://{server}/api/cluster/join",
                json={
                    "csr": csr.decode(),
                    "proof": ClusterCA.proof(secret, csr),
                    "token_hash": hashlib.sha256(secret).hexdigest(),
                    "node": info,
                },
            )
        if resp.status_code != 200:
            raise EnrollmentError(resp.json().get("detail", "join refused"))
        data: dict[str, Any] = resp.json()
        _check_issued(ca_pem, data["cert"].encode(), key, node_id)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write("ca.crt", ca_pem)
        self._write("node.key", key_pem(key), secret=True)
        self._write("node.crt", data["cert"].encode())
        if data.get("data_key"):
            from opstend.secrets import DataKey, open_sealed

            DataKey(open_sealed(key, data["data_key"])).save(self.path("data.key"))
        return data


def _check_issued(ca_pem: bytes, cert_pem: bytes, key: Any, node_id: str) -> None:
    from cryptography import x509

    from opstend.cluster.enrollment import public_fingerprint

    ca = x509.load_pem_x509_certificate(ca_pem)
    cert = x509.load_pem_x509_certificate(cert_pem)
    try:
        cert.verify_directly_issued_by(ca)
    except Exception as exc:
        raise EnrollmentError("the returned certificate is not from the pinned cluster CA") from exc
    if public_fingerprint(cert.public_key()) != public_fingerprint(key):  # type: ignore[arg-type]
        raise EnrollmentError("the returned certificate is not for this node's key")
    cn = cert.subject.rfc4514_string()
    if f"CN={node_id}" not in cn:
        raise EnrollmentError("the returned certificate names another node")


def _fingerprint(pem: bytes) -> str:
    from opstend.cluster.enrollment import fingerprint

    return fingerprint(pem)


class JoinRequest(BaseModel):
    csr: str
    proof: str
    token_hash: str
    node: dict[str, Any]


def web_routes(app: FastAPI, rt: Runtime) -> None:
    """Join endpoints on the web port (TLS, no session: the token proves the right)."""

    @app.get("/api/cluster/ca")
    async def cluster_ca() -> Any:
        from fastapi.responses import Response

        path = rt.identity.path("ca.crt") if rt.identity else None
        if path is None or not path.exists():
            raise HTTPException(404, "no cluster yet")
        return Response(path.read_bytes(), media_type="application/x-pem-file")

    @app.post("/api/cluster/join")
    async def cluster_join(body: JoinRequest) -> dict[str, Any]:
        ca = rt.identity.ca() if rt.identity else None
        if ca is None:
            raise HTTPException(409, "this node cannot sign; send the request to a voter")
        token = rt.store.get("join_token", body.token_hash)
        if token is None or datetime.fromisoformat(token["expires"]) < datetime.now(UTC):
            raise HTTPException(403, "join token is unknown, used or expired")
        ca.remember_token(body.token_hash, datetime.fromisoformat(token["expires"]))
        try:
            cert = (
                ca.redeem(body.csr.encode(), body.proof, body.token_hash)
                if _proof_ok(body, token)
                else None
            )
        except EnrollmentError as exc:
            raise HTTPException(403, str(exc)) from exc
        if cert is None:
            raise HTTPException(403, "token proof does not match the request")
        node_id = ca.verify_node_cert(cert)
        await rt.write({"op": "delete", "kind": "join_token", "id": body.token_hash})
        info = body.node
        await rt.write(
            {
                "op": "put",
                "kind": "node",
                "id": node_id,
                "data": {
                    "id": node_id,
                    "name": str(info.get("name", node_id))[:64],
                    "address": str(info.get("address", ""))[:255],
                    "version": str(info.get("version", "")),
                    "state": "joining",
                    "voter": False,
                    "site": str(info.get("site", "default"))[:64],
                    "labels": {k: str(v)[:64] for k, v in dict(info.get("labels", {})).items()},
                    "capacity": info.get("capacity", {}),
                    "usage": {},
                    "last_seen": datetime.now(UTC).isoformat(),
                    "host_id": None,
                },
            }
        )
        peers = json.loads(rt.store.meta("peers") or "{}")
        peers[node_id] = f"https://{info.get('address')}:{info.get('cluster_port', 47471)}"
        await rt.write({"op": "meta", "key": "peers", "value": json.dumps(peers)})
        rt.audit.record("cluster", "node.join", f"node/{node_id}")
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        from opstend.secrets import seal_for

        node_key = (
            x509.load_pem_x509_certificate(cert)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        return {
            "cert": cert.decode(),
            "peers": peers,
            "voters": rt.raft.voters,
            "node_id": node_id,
            "data_key": seal_for(node_key, rt.data_key.key),
        }


def _proof_ok(body: JoinRequest, token: dict[str, Any]) -> bool:
    import hmac

    secret = bytes.fromhex(token["secret"])
    return hmac.compare_digest(ClusterCA.proof(secret, body.csr.encode()), body.proof)


def cluster_app(rt: Runtime) -> FastAPI:
    """Endpoints on the cluster port. The TLS layer already required a CA certificate."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/cluster/raft/vote")
    async def vote(body: dict[str, Any]) -> dict[str, Any]:
        return asdict(await rt.raft.handle_vote(VoteRequest(**body)))

    @app.post("/cluster/raft/append")
    async def append(body: dict[str, Any]) -> dict[str, Any]:
        return asdict(await rt.raft.handle_append(decode_append(body)))

    @app.post("/cluster/raft/snapshot")
    async def snapshot(body: dict[str, Any]) -> dict[str, Any]:
        return asdict(await rt.raft.handle_snapshot(SnapshotRequest(**body)))

    @app.post("/cluster/propose")
    async def propose(body: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await rt.raft.propose(body)
        except NotLeader as exc:
            raise HTTPException(421, f"leader is {exc.leader}") from exc
        return {"result": result}

    @app.post("/cluster/self-update")
    async def self_update(body: dict[str, Any]) -> dict[str, Any]:
        """The leader asks this node to update itself during a coordinated rollout."""
        from opstend.agent import AgentRunner

        version = str(body.get("version", ""))
        result = await AgentRunner(timeout=900).run(
            ["/opt/opstend/current/bin/opstend", "self-update", "--version", version], check=False
        )
        return {"code": result.code, "output": (result.stdout + result.stderr)[-2000:]}

    @app.post("/cluster/heartbeat")
    async def heartbeat(body: dict[str, Any]) -> dict[str, Any]:
        node = rt.store.get("node", str(body.get("id")))
        if node is None:
            raise HTTPException(404, "unknown node")
        node.update(
            last_seen=datetime.now(UTC).isoformat(),
            usage=body.get("usage", {}),
            version=str(body.get("version", node.get("version", ""))),
        )
        if node.get("state") == "joining":
            node["state"] = "healthy"
        await rt.write({"op": "put", "kind": "node", "id": node["id"], "data": node})
        return {"leader": rt.raft.leader_id, "term": rt.raft.term}

    return app
