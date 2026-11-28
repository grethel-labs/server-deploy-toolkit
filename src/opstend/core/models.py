"""Shared domain model.

Every object that OpsTend shows or acts on is one of these types. Adapters
translate native APIs (Proxmox, Podman, libvirt, UniFi) into this model, so the
UI, the automation and Zorec work the same way on every platform.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opstend import i18n


def utcnow() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=True)


class Localized(Model):
    """A stored object whose texts can be rendered in each reader's language."""

    i18n: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _capture_texts(cls, data: Any) -> Any:
        if isinstance(data, dict):
            found = i18n.capture(data)
            if found:
                data = {**data, "i18n": {**data.get("i18n", {}), **found}}
        return data

    def remember_texts(self, *fields: str) -> None:
        """Keep templates of texts assigned after construction."""
        self.i18n.update(i18n.capture({f: getattr(self, f) for f in fields}))


class Health(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    MAINTENANCE = "maintenance"


class Observed(Model):
    """When and from where a fact was read. Stale facts are shown as stale."""

    source: str
    observed_at: datetime = Field(default_factory=utcnow)
    max_age_s: int = 300

    def is_stale(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return (now - self.observed_at).total_seconds() > self.max_age_s


class Resources(Model):
    cpu_cores: float = 0
    memory_mb: int = 0
    disk_gb: int = 0
    gpu: int = 0


class Usage(Model):
    cpu_pct: float | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None


# --- Cluster ---------------------------------------------------------------


class NodeState(StrEnum):
    JOINING = "joining"
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    DOWN = "down"
    DRAINING = "draining"
    LEFT = "left"


class Node(Model):
    """A machine running the OpsTend agent."""

    id: str
    name: str
    address: str
    version: str = ""
    state: NodeState = NodeState.JOINING
    voter: bool = False
    capacity: Resources = Field(default_factory=Resources)
    usage: Usage = Field(default_factory=Usage)
    labels: dict[str, str] = Field(default_factory=dict)
    site: str = "default"
    last_seen: datetime = Field(default_factory=utcnow)
    host_id: str | None = None


# --- Compute ---------------------------------------------------------------


class Platform(StrEnum):
    PROXMOX = "proxmox"
    LINUX = "linux"
    UNIFI = "unifi"


class Host(Model):
    """A managed system: a Proxmox node, a Linux server or a UniFi console."""

    id: str
    name: str
    platform: Platform
    address: str
    site: str = "default"
    os: str = ""
    version: str = ""
    health: Health = Health.UNKNOWN
    capacity: Resources = Field(default_factory=Resources)
    usage: Usage = Field(default_factory=Usage)
    cluster: str | None = None
    managed_by: str | None = None
    observed: Observed | None = None
    tags: list[str] = Field(default_factory=list)


class GuestKind(StrEnum):
    VM = "vm"
    LXC = "lxc"
    CONTAINER = "container"


class GuestState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class Nic(Model):
    name: str
    mac: str | None = None
    bridge: str | None = None
    vlan: int | None = None
    ips: list[str] = Field(default_factory=list)
    firewall: bool = False


class Snapshot(Model):
    name: str
    created_at: datetime | None = None
    description: str = ""


class Guest(Model):
    """A VM, LXC or container. The same actions apply on every platform."""

    id: str
    host_id: str
    kind: GuestKind
    native_id: str
    name: str
    state: GuestState = GuestState.UNKNOWN
    capacity: Resources = Field(default_factory=Resources)
    usage: Usage = Field(default_factory=Usage)
    nics: list[Nic] = Field(default_factory=list)
    snapshots: list[Snapshot] = Field(default_factory=list)
    template: bool = False
    tags: list[str] = Field(default_factory=list)
    image: str | None = None
    uptime_s: int | None = None
    observed: Observed | None = None


# --- Network ---------------------------------------------------------------


class DeviceKind(StrEnum):
    GATEWAY = "gateway"
    SWITCH = "switch"
    ACCESS_POINT = "ap"
    OTHER = "other"


class Lldp(Model):
    chassis: str
    port: str
    name: str | None = None


class Port(Model):
    index: int
    name: str
    up: bool = False
    speed_mbps: int | None = None
    max_speed_mbps: int | None = None
    full_duplex: bool = True
    poe_watts: float | None = None
    poe_enabled: bool = False
    native_vlan: int | None = None
    tagged_vlans: list[int] = Field(default_factory=list)
    profile: str | None = None
    rx_errors: int = 0
    tx_errors: int = 0
    lldp: Lldp | None = None
    is_uplink: bool = False


class Device(Model):
    """A network device such as a gateway, switch or access point."""

    id: str
    name: str
    kind: DeviceKind
    mac: str
    model: str = ""
    address: str | None = None
    firmware: str = ""
    upgrade_to: str | None = None
    health: Health = Health.UNKNOWN
    ports: list[Port] = Field(default_factory=list)
    uplink_mac: str | None = None
    uplink_port: int | None = None
    poe_budget_watts: float | None = None
    site: str = "default"
    observed: Observed | None = None


class Network(Model):
    id: str
    name: str
    vlan: int | None = None
    subnet: str | None = None
    gateway: str | None = None
    dhcp_start: str | None = None
    dhcp_stop: str | None = None
    purpose: str = "corporate"
    isolated: bool = False
    source: str = "unifi"


class Client(Model):
    mac: str
    name: str | None = None
    ip: str | None = None
    network_id: str | None = None
    vlan: int | None = None
    device_mac: str | None = None
    port: int | None = None
    wired: bool = True
    signal_dbm: int | None = None
    guest_id: str | None = None
    last_seen: datetime | None = None


class RuleAction(StrEnum):
    ALLOW = "allow"
    DROP = "drop"


class FirewallRule(Model):
    id: str
    name: str
    action: RuleAction
    src: str = "any"
    dst: str = "any"
    protocol: str = "any"
    ports: str | None = None
    enabled: bool = True
    order: int = 1000
    source: str = "unifi"


class Tunnel(Model):
    id: str
    name: str
    kind: str = "wireguard"
    a_host: str
    b_host: str
    networks: list[str] = Field(default_factory=list)
    up: bool = True
    handshake_at: datetime | None = None


class Publication(Model):
    """A service made reachable from another network, usually via HTTPS."""

    id: str
    hostname: str
    service_id: str
    proxy_host: str
    target: str
    tunnel_id: str | None = None
    tls: bool = True
    public: bool = False


# --- Services and deployment ----------------------------------------------


class Service(Model):
    id: str
    name: str
    description: str = ""
    guest_id: str | None = None
    host_id: str | None = None
    health: Health = Health.UNKNOWN
    endpoints: list[str] = Field(default_factory=list)
    spec: dict[str, Any] | None = None
    core: bool = False
    version: str | None = None
    exposed: bool = False


# --- Security ---------------------------------------------------------------


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingKind(StrEnum):
    VULNERABILITY = "vulnerability"
    HARDENING = "hardening"
    EXPOSURE = "exposure"
    NETWORK = "network"
    CONFIGURATION = "configuration"


class Finding(Localized):
    id: str
    target: str
    kind: FindingKind
    severity: Severity
    title: str
    detail: str = ""
    cve: str | None = None
    package: str | None = None
    installed: str | None = None
    fixed: str | None = None
    kev: bool = False
    epss: float | None = None
    exposed: bool = False
    first_seen: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    recommendation: str | None = None


class Update(Model):
    """An available update for any target: OS, Proxmox, firmware, app or OpsTend."""

    id: str
    target: str
    kind: str
    current: str
    available: str
    security: bool = False
    reboot: bool = False
    notes: str = ""
