"""Node configuration (`/etc/opstend/opstend.yaml`).

Secrets never live in this file. Integrations point to files under
`/etc/opstend/secrets/` (mode 0600, owned by the opstend user) or to
systemd credentials.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeConfig(Strict):
    id: str = Field(default_factory=lambda: f"node-{socket.gethostname()}")
    name: str = Field(default_factory=socket.gethostname)
    address: str = "127.0.0.1"
    site: str = "default"
    labels: dict[str, str] = Field(default_factory=dict)


class Listen(Strict):
    web: str = "127.0.0.1:8443"
    cluster: str = "0.0.0.0:47471"


class ClusterConfig(Strict):
    name: str = "opstend"
    peers: dict[str, str] = Field(default_factory=dict)  # node id -> https://addr:47471
    voters: list[str] = Field(default_factory=list)
    join_token_file: str = "/etc/opstend/secrets/join-token"  # noqa: S105 - a path


class ProxmoxIntegration(Strict):
    name: str
    url: str
    token_id: str
    token_secret_file: str
    cluster: str = "pve"
    cert_file: str | None = None
    site: str = "default"


class UnifiIntegration(Strict):
    name: str
    url: str
    username: str
    password_file: str
    site: str = "default"
    unifi_site: str = "default"
    unifi_os: bool = True
    cert_file: str | None = None


class LinuxIntegration(Strict):
    name: str
    address: str
    ssh_user: str = "opstend"
    ssh_key_file: str = "/etc/opstend/secrets/ssh_ed25519"
    known_hosts: str = "/etc/opstend/known_hosts"
    site: str = "default"
    local: bool = False


class Integrations(Strict):
    proxmox: list[ProxmoxIntegration] = Field(default_factory=list)
    unifi: list[UnifiIntegration] = Field(default_factory=list)
    linux: list[LinuxIntegration] = Field(default_factory=list)


class ZorecConfig(Strict):
    provider: str = "none"  # none | anthropic | local
    model: str | None = None
    url: str | None = None
    api_key_file: str | None = None


class Config(Strict):
    node: NodeConfig = Field(default_factory=NodeConfig)
    data_dir: str = "/var/lib/opstend"
    listen: Listen = Field(default_factory=Listen)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    integrations: Integrations = Field(default_factory=Integrations)
    zorec: ZorecConfig = Field(default_factory=ZorecConfig)
    discovery_subnets: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        if path is None or not Path(path).exists():
            return cls()
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)


def read_secret(path: str | None) -> str | None:
    """Read a secret file and refuse files other users can read."""
    if not path:
        return None
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    candidate = (
        Path(creds) / Path(path).name if creds and not Path(path).is_absolute() else Path(path)
    )
    st = candidate.stat()
    if st.st_mode & 0o077:
        raise PermissionError(f"{candidate} must not be readable by group or others (chmod 600)")
    return candidate.read_text().strip()
