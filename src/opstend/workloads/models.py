"""Workloads: isolated applications with explicit resources, devices and network paths.

A workload is the unit people install, test, run and remove. Every workload:

* runs in its own network; nothing else can reach it unless a `Connection` says so,
* has no internet access unless its egress policy allows named hosts or the internet,
* is reachable only internally, on the LAN or publicly through HTTPS, as chosen,
* declares CPU, memory and storage, and may request devices (GPU, USB, display,
  serial); devices always need explicit approval,
* can run as a time-limited sandbox first (no internet, no real data), then be
  promoted to production.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from opstend.core.models import Localized, Model, utcnow
from opstend.deploy.spec import Exposure, ServiceSpec

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


class Environment(StrEnum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"


class WorkloadState(StrEnum):
    DRAFT = "draft"
    DEPLOYING = "deploying"
    TESTING = "testing"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    EXPIRED = "expired"
    REMOVED = "removed"


class DeviceKind(StrEnum):
    GPU = "gpu"  # render node or NVIDIA via CDI
    USB = "usb"  # a USB device by vendor:product
    DISPLAY = "display"  # DRM card for HDMI/DP output
    SERIAL = "serial"  # /dev/ttyUSB*, /dev/ttyACM*
    AUDIO = "audio"


class DeviceRequest(Model):
    kind: DeviceKind
    selector: str = ""  # vendor:product for USB, "nvidia"/"intel"/"amd" for GPU, path otherwise
    optional: bool = False
    reason: str = ""


class Egress(StrEnum):
    NONE = "none"
    ALLOWLIST = "allowlist"
    INTERNET = "internet"


class NetworkPolicy(Model):
    egress: Egress = Egress.NONE
    egress_hosts: list[str] = Field(default_factory=list)

    @field_validator("egress_hosts")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not re.fullmatch(
                r"(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", host
            ):
                raise ValueError(f"{host!r} is not a host name")
        return value


class Connection(Model):
    """An explicit path: services of this workload may reach `service:port` of another."""

    to_workload: str
    to_service: str
    port: int
    reason: str = ""


class Resources(Model):
    cpus: float = 1.0
    memory_mb: int = 512
    storage_gb: int = 5


class Workload(Localized):
    id: str
    name: str
    adapter: str | None = None  # catalog id
    adapter_version: str | None = None
    environment: Environment = Environment.PRODUCTION
    state: WorkloadState = WorkloadState.DRAFT
    host_id: str | None = None
    services: list[ServiceSpec] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)  # env name -> secret id
    files: list[dict[str, str]] = Field(default_factory=list)  # service, path, content
    resources: Resources = Field(default_factory=Resources)
    devices: list[DeviceRequest] = Field(default_factory=list)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    connections: list[Connection] = Field(default_factory=list)
    expires_at: datetime | None = None  # sandboxes only
    sandbox_of: str | None = None
    tests: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)
    message: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ID.fullmatch(value):
            raise ValueError("ids use lowercase letters, digits and dashes")
        return value

    @property
    def network_name(self) -> str:
        return f"opstend-wl-{self.id}"

    def container(self, service: str) -> str:
        return f"{self.id}-{service}"

    def exposure(self) -> Exposure:
        levels = [p.exposure for s in self.services for p in s.ports]
        for level in (Exposure.PUBLIC, Exposure.LAN):
            if level in levels:
                return level
        return Exposure.INTERNAL

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or datetime.now(UTC)) >= self.expires_at


def sandbox_from(source: Workload, hours: int = 4) -> Workload:
    """A copy for testing: own id, no internet, no ingress beyond the LAN, time-limited."""
    data = source.model_dump()
    data.update(
        id=f"{source.id[:30]}-sbx",
        environment=Environment.SANDBOX,
        state=WorkloadState.DRAFT,
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
        sandbox_of=source.id,
        network=NetworkPolicy(egress=Egress.NONE).model_dump(),
        connections=[],
        tests=[{k: v for k, v in t.items() if k not in ("ok", "detail")} for t in source.tests],
        name=f"{source.name} (Sandbox)",
        i18n={},
    )
    for svc in data["services"]:
        for port in svc["ports"]:
            if port["exposure"] == Exposure.PUBLIC:
                port["exposure"] = Exposure.LAN
                port["hostname"] = None
    return Workload.model_validate(data)


def promote(sandbox: Workload) -> Workload:
    """The production workload a sandbox was tested for."""
    if sandbox.environment != Environment.SANDBOX:
        raise ValueError("only sandboxes can be promoted")
    data = sandbox.model_dump()
    data.update(
        id=sandbox.sandbox_of or sandbox.id.removesuffix("-sbx"),
        environment=Environment.PRODUCTION,
        state=WorkloadState.DRAFT,
        expires_at=None,
        sandbox_of=None,
        name=sandbox.name.replace(" (Sandbox)", ""),
        i18n={},
    )
    return Workload.model_validate(data)
