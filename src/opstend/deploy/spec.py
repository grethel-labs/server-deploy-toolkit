"""Workload specification and its safety validation.

Every deployment, whether drafted by Zorec or written by hand, ends up as a
`WorkloadSpec`. The validator enforces the same isolation floor for all of
them; a model can make suggestions but cannot relax these rules.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, field_validator

from opstend.core.models import Model
from opstend.i18n import tr


class Exposure(StrEnum):
    INTERNAL = "internal"  # only other services of this workload
    LAN = "lan"  # reachable from the local networks
    PUBLIC = "public"  # published through the HTTPS proxy


class PortSpec(Model):
    container: int
    protocol: str = "tcp"
    exposure: Exposure = Exposure.INTERNAL
    hostname: str | None = None


class VolumeSpec(Model):
    name: str
    path: str
    backup: bool = True


class EnvVar(Model):
    name: str
    value: str | None = None
    secret: bool = False  # stored in the secret store, injected at start
    required: bool = False


class HealthSpec(Model):
    kind: str = "http"
    path: str = "/"
    port: int | None = None


class ServiceSpec(Model):
    name: str
    image: str | None = None
    build_context: str | None = None
    dockerfile: str | None = None
    command: list[str] | None = None
    ports: list[PortSpec] = Field(default_factory=list)
    volumes: list[VolumeSpec] = Field(default_factory=list)
    env: list[EnvVar] = Field(default_factory=list)
    health: HealthSpec | None = None
    cpus: float = 1.0
    memory_mb: int = 512
    user: str | None = None
    read_only: bool = True
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", v):
            raise ValueError("service names use lowercase letters, digits and dashes")
        return v


class WorkloadSpec(Model):
    name: str
    source: str | None = None  # repository URL
    revision: str | None = None  # exact commit
    services: list[ServiceSpec]
    architectures: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class Violation(Model):
    service: str
    rule: str
    message: str
    blocking: bool = True


FORBIDDEN_PATHS = (
    "/",
    "/etc",
    "/root",
    "/var/run/docker.sock",
    "/run/podman",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/home",
)


def validate(spec: WorkloadSpec, *, allow_public: bool = False) -> list[Violation]:
    out: list[Violation] = []
    names = {s.name for s in spec.services}
    for s in spec.services:
        if not s.image and not s.build_context:
            out.append(
                Violation(
                    service=s.name,
                    rule="source",
                    message=tr("needs an image or a build context"),
                )
            )
        if s.image and (":" not in s.image.rsplit("/", 1)[-1] and "@sha256:" not in s.image):
            out.append(
                Violation(
                    service=s.name,
                    rule="pinned-image",
                    blocking=False,
                    message=tr("image has no tag; OpsTend pins it to a digest on deploy"),
                )
            )
        if s.image and s.image.endswith(":latest"):
            out.append(
                Violation(
                    service=s.name,
                    rule="pinned-image",
                    blocking=False,
                    message=tr("'latest' changes silently; OpsTend pins the digest"),
                )
            )
        for v in s.volumes:
            if v.path.rstrip("/") in FORBIDDEN_PATHS or not v.path.startswith("/"):
                out.append(
                    Violation(
                        service=s.name,
                        rule="volume-path",
                        message=tr("volume path {v_path} is not allowed", v_path=v.path),
                    )
                )
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", v.name):
                out.append(
                    Violation(
                        service=s.name,
                        rule="volume-name",
                        message=tr("volume name {v_name!r} must be a plain name", v_name=v.name),
                    )
                )
        for p in s.ports:
            if not 1 <= p.container <= 65535:
                out.append(
                    Violation(
                        service=s.name,
                        rule="port",
                        message=tr("invalid port {container}", container=p.container),
                    )
                )
            if p.exposure == Exposure.PUBLIC:
                if not p.hostname:
                    out.append(
                        Violation(
                            service=s.name,
                            rule="public-hostname",
                            message=tr("public ports need a hostname for HTTPS"),
                        )
                    )
                if not allow_public:
                    out.append(
                        Violation(
                            service=s.name,
                            rule="public-approval",
                            blocking=False,
                            message=tr("public exposure needs operator approval"),
                        )
                    )
        if not 0.05 <= s.cpus <= 64:
            out.append(
                Violation(
                    service=s.name,
                    rule="limits",
                    message=tr("cpus must be 0.05–64"),
                )
            )
        if not 32 <= s.memory_mb <= 262144:
            out.append(
                Violation(
                    service=s.name,
                    rule="limits",
                    message=tr("memory must be 32 MB–256 GB"),
                )
            )
        if s.user in ("root", "0", "0:0"):
            out.append(
                Violation(
                    service=s.name,
                    rule="non-root",
                    blocking=False,
                    message=tr(
                        "runs as root inside the container; it still has no host privileges (rootless, user namespace)"
                    ),
                )
            )
        for dep in s.depends_on:
            if dep not in names:
                out.append(
                    Violation(
                        service=s.name,
                        rule="depends-on",
                        message=tr("depends on unknown service {dep}", dep=dep),
                    )
                )
        for e in s.env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", e.name):
                out.append(
                    Violation(
                        service=s.name,
                        rule="env",
                        message=tr("bad variable {e_name}", e_name=e.name),
                    )
                )
            if e.secret and e.value:
                out.append(
                    Violation(
                        service=s.name,
                        rule="secret-inline",
                        message=tr(
                            "{e_name} is a secret and must not carry a value", e_name=e.name
                        ),
                    )
                )
    return out


def podman_args(workload: str, s: ServiceSpec, image_ref: str) -> list[str]:
    """Hardened `podman run` arguments. Every container gets the same floor."""
    args = [
        "podman",
        "run",
        "--detach",
        "--replace",
        "--name",
        f"{workload}-{s.name}",
        "--label",
        f"opstend.workload={workload}",
        "--label",
        f"opstend.service={s.name}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--userns=auto",
        f"--cpus={s.cpus}",
        f"--memory={s.memory_mb}m",
        "--pids-limit=512",
        f"--network={workload}",
        "--restart=on-failure:5",
    ]
    if s.read_only:
        args += ["--read-only", "--tmpfs=/tmp:rw,size=64m"]
    for v in s.volumes:
        args += ["--volume", f"{workload}-{v.name}:{v.path}:Z"]
    for e in s.env:
        if e.secret:
            args += ["--secret", f"{workload}-{e.name},type=env,target={e.name}"]
        elif e.value is not None:
            args += ["--env", f"{e.name}={e.value}"]
    for p in s.ports:
        if p.exposure != Exposure.INTERNAL:
            # Published ports bind to the service network address chosen at placement.
            args += ["--publish", f"{{bind}}:{p.container}:{p.container}/{p.protocol}"]
    if s.health and s.health.kind == "http":
        port = s.health.port or (s.ports[0].container if s.ports else 80)
        args += ["--health-cmd", f"wget -q -O /dev/null http://127.0.0.1:{port}{s.health.path}"]
    args.append(image_ref)
    if s.command:
        args += s.command
    return args
