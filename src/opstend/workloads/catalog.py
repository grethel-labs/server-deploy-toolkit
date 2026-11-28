"""Adapters: versioned descriptions of applications.

An adapter says everything OpsTend needs to run, check, back up and update an
application safely: images, settings (with secrets), ports and their default
exposure, data volumes, resources, optional devices, outbound hosts it needs,
health checks and smoke tests. Built-in adapters ship in `opstend/catalog/`;
Zorec drafts new ones from repositories, which are tested in a sandbox before
anyone can use them in production.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from opstend.core.models import Model
from opstend.deploy.spec import EnvVar, Exposure, HealthSpec, PortSpec, ServiceSpec, VolumeSpec
from opstend.workloads.models import DeviceRequest, Egress, NetworkPolicy, Resources, Workload

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


class SettingType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SECRET = "secret"  # noqa: S105 - a setting type, not a password
    SELECT = "select"


class Setting(Model):
    name: str
    label: str
    type: SettingType = SettingType.STRING
    default: Any = None
    required: bool = False
    help: str = ""
    options: list[str] = Field(default_factory=list)
    env: str | None = None  # which environment variable receives it
    service: str | None = None  # which service; all services when None


class ConfigFile(Model):
    service: str
    path: str
    content: str  # may contain {setting} placeholders


class SmokeTest(Model):
    name: str
    service: str
    port: int
    path: str = "/"
    expect_status: list[int] = Field(default_factory=lambda: [200])
    expect_text: str | None = None


class AdapterStatus(StrEnum):
    CURATED = "curated"  # shipped with OpsTend
    DRAFT = "draft"  # drafted by Zorec or a person, not tested
    TESTED = "tested"  # passed its smoke tests in a sandbox
    APPROVED = "approved"  # a person approved it for production


class Adapter(Model):
    id: str
    name: str
    version: str
    description: str
    category: str = "other"
    homepage: str | None = None
    status: AdapterStatus = AdapterStatus.CURATED
    services: list[ServiceSpec]
    settings: list[Setting] = Field(default_factory=list)
    files: list[ConfigFile] = Field(default_factory=list)
    resources: Resources = Field(default_factory=Resources)
    devices: list[DeviceRequest] = Field(default_factory=list)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    tests: list[SmokeTest] = Field(default_factory=list)
    backup_paths: list[str] = Field(default_factory=list)
    update: str = "minor"  # none | patch | minor | major
    architectures: list[str] = Field(default_factory=lambda: ["amd64", "arm64"])
    notes: list[str] = Field(default_factory=list)
    origin: str | None = None  # repository the adapter was drafted from

    def instantiate(
        self, workload_id: str, name: str, values: dict[str, Any], secret_ids: dict[str, str]
    ) -> Workload:
        """A workload from this adapter with the given setting values and secret ids."""
        services = [s.model_copy(deep=True) for s in self.services]
        by_name = {s.name: s for s in services}
        secrets: dict[str, str] = {}
        settings: dict[str, Any] = {}
        for setting in self.settings:
            targets = [by_name[setting.service]] if setting.service else services
            if setting.type == SettingType.SECRET:
                if setting.name in secret_ids:
                    secrets[setting.env or setting.name] = secret_ids[setting.name]
                    for svc in targets:
                        if setting.env and not any(e.name == setting.env for e in svc.env):
                            svc.env.append(EnvVar(name=setting.env, secret=True, required=True))
                elif setting.required:
                    raise ValueError(f"secret {setting.name} is required")
                continue
            value = values.get(setting.name, setting.default)
            if value is None:
                if setting.required:
                    raise ValueError(f"setting {setting.name} is required")
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            settings[setting.name] = value
            if setting.env:
                for svc in targets:
                    svc.env = [e for e in svc.env if e.name != setting.env]
                    svc.env.append(EnvVar(name=setting.env, value=str(value)))
        files = [
            {
                "service": f.service,
                "path": f.path,
                "content": f.content.format(
                    **{s.name: settings.get(s.name, "") for s in self.settings}
                ),
            }
            for f in self.files
        ]
        return Workload(
            id=workload_id,
            name=name,
            adapter=self.id,
            adapter_version=self.version,
            services=services,
            settings=settings,
            secrets=secrets,
            files=files,
            resources=self.resources,
            devices=self.devices,
            network=self.network,
            tests=[t.model_dump() for t in self.tests],
        )


def load_builtin() -> dict[str, Adapter]:
    out = {}
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        adapter = Adapter.model_validate(yaml.safe_load(path.read_text()))
        out[adapter.id] = adapter
    return out


def adapter_from_spec(spec: Any, origin: str | None) -> Adapter:
    """Draft an adapter from an analyzed repository (deploy.analyzer.WorkloadSpec)."""
    settings: list[Setting] = []
    for svc in spec.services:
        for env in svc.env:
            settings.append(
                Setting(
                    name=env.name.lower(),
                    label=env.name,
                    env=env.name,
                    service=svc.name,
                    type=SettingType.SECRET if env.secret else SettingType.STRING,
                    default=None if env.secret else env.value,
                    required=env.required,
                )
            )
        svc.env = [e for e in svc.env if not e.secret and e.value is not None]
    tests = [
        SmokeTest(
            name=f"{s.name} answers",
            service=s.name,
            port=(s.health.port if s.health and s.health.port else s.ports[0].container),
            path=s.health.path if s.health else "/",
            expect_status=[200, 301, 302, 401],
        )
        for s in spec.services
        if s.ports
    ]
    return Adapter(
        id=spec.name,
        name=spec.name.replace("-", " ").title(),
        version="0.1.0",
        description=f"Drafted from {origin}" if origin else "Drafted adapter",
        status=AdapterStatus.DRAFT,
        services=spec.services,
        settings=settings,
        network=NetworkPolicy(egress=Egress.NONE),
        tests=tests,
        notes=list(spec.notes),
        origin=origin,
    )


__all__ = [
    "Adapter",
    "AdapterStatus",
    "ConfigFile",
    "Exposure",
    "HealthSpec",
    "PortSpec",
    "Setting",
    "SettingType",
    "SmokeTest",
    "VolumeSpec",
    "adapter_from_spec",
    "load_builtin",
]
