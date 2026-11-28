"""Turn a repository into a workload proposal.

The analyzer reads only declarative files (Compose, Containerfile/Dockerfile,
package manifests, .env.example, README) from an exact revision. It never runs
code from the repository. The result is a draft `WorkloadSpec` with open
questions; Zorec can refine wording and defaults, the validator decides.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from opstend.deploy.spec import (
    EnvVar,
    Exposure,
    HealthSpec,
    PortSpec,
    ServiceSpec,
    VolumeSpec,
    WorkloadSpec,
)
from opstend.i18n import tr

SECRET_HINT = re.compile(r"(PASS(WORD)?|SECRET|TOKEN|API_?KEY|PRIVATE|CREDENTIAL|DSN)", re.I)
WEB_PORTS = {80, 443, 3000, 5000, 8000, 8080, 8443, 9000}

FRAMEWORKS = [
    # (manifest, pattern, port, health path, note)
    ("package.json", r'"next"\s*:', 3000, "/", "Next.js app"),
    ("package.json", r'"express"\s*:', 3000, "/", "Node.js (Express) app"),
    ("pyproject.toml", r"fastapi", 8000, "/docs", "FastAPI app"),
    ("requirements.txt", r"(?im)^fastapi", 8000, "/docs", "FastAPI app"),
    ("requirements.txt", r"(?im)^flask", 5000, "/", "Flask app"),
    ("requirements.txt", r"(?im)^django", 8000, "/", "Django app"),
    ("go.mod", r"^module ", 8080, "/", "Go service"),
    ("Cargo.toml", r"\[package\]", 8080, "/", "Rust service"),
]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return (s or "app")[:40]


def parse_env_example(text: str) -> list[EnvVar]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        secret = bool(SECRET_HINT.search(name))
        out.append(
            EnvVar(
                name=name,
                value=None if secret else (value.strip().strip('"') or None),
                secret=secret,
                required=not value.strip(),
            )
        )
    return out


def _compose_ports(raw: list[Any]) -> list[PortSpec]:
    ports = []
    for item in raw or []:
        if isinstance(item, dict):
            target = int(item.get("target", 0))
            proto = item.get("protocol", "tcp")
        else:
            text = str(item)
            proto = "udp" if text.endswith("/udp") else "tcp"
            target = int(text.split("/")[0].split(":")[-1].split("-")[0])
        if target:
            exposure = Exposure.LAN if target in WEB_PORTS else Exposure.INTERNAL
            ports.append(PortSpec(container=target, protocol=proto, exposure=exposure))
    return ports


def _compose_env(raw: Any) -> list[EnvVar]:
    items: dict[str, Any] = {}
    if isinstance(raw, dict):
        items = raw
    elif isinstance(raw, list):
        for entry in raw:
            k, _, v = str(entry).partition("=")
            items[k] = v or None
    out = []
    for name, value in items.items():
        secret = bool(SECRET_HINT.search(name))
        is_ref = isinstance(value, str) and value.startswith("${")
        out.append(
            EnvVar(
                name=name,
                value=None if secret or is_ref else (str(value) if value is not None else None),
                secret=secret,
                required=value is None or is_ref,
            )
        )
    return out


def _compose_volumes(raw: list[Any], service: str, notes: list[str]) -> list[VolumeSpec]:
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            source, target = item.get("source", ""), item.get("target", "")
        else:
            parts = str(item).split(":")
            source, target = (parts[0], parts[1]) if len(parts) >= 2 else ("", parts[0])
        if not target:
            continue
        if source.startswith(("/", ".", "~")):
            notes.append(f"{service}: host path {source} becomes a managed volume")
        name = _slug(source.rsplit("/", 1)[-1] or target.rsplit("/", 1)[-1] or "data")
        out.append(VolumeSpec(name=name, path=target, backup=True))
    return out


def from_compose(text: str, name: str) -> WorkloadSpec:
    doc = yaml.safe_load(text) or {}
    services: list[ServiceSpec] = []
    notes: list[str] = []
    questions: list[str] = []
    for sname, svc in (doc.get("services") or {}).items():
        svc = svc or {}
        build = svc.get("build")
        context = build if isinstance(build, str) else (build or {}).get("context")
        dockerfile = None if isinstance(build, str) else (build or {}).get("dockerfile")
        health = None
        hc = svc.get("healthcheck") or {}
        test = (
            " ".join(hc.get("test", [])) if isinstance(hc.get("test"), list) else hc.get("test", "")
        )
        m = re.search(r"https?://(?:localhost|127\.0\.0\.1)(?::(\d+))?(/\S*)?", str(test))
        if m:
            health = HealthSpec(
                kind="http", path=m.group(2) or "/", port=int(m.group(1)) if m.group(1) else None
            )
        if svc.get("privileged"):
            notes.append(
                tr(
                    "{sname}: 'privileged' is dropped; OpsTend runs rootless without capabilities",
                    sname=sname,
                )
            )
        if svc.get("network_mode") == "host":
            notes.append(
                tr("{sname}: host networking is replaced by a service network", sname=sname)
            )
        limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        mem = str(limits.get("memory", "512M")).upper()
        mem_mb = int(re.sub(r"\D", "", mem) or 512) * (1024 if mem.endswith("G") else 1)
        ports = _compose_ports(svc.get("ports") or svc.get("expose") or [])
        service = ServiceSpec(
            name=_slug(sname),
            image=svc.get("image"),
            build_context=context,
            dockerfile=dockerfile,
            command=svc["command"] if isinstance(svc.get("command"), list) else None,
            ports=ports,
            volumes=_compose_volumes(svc.get("volumes") or [], sname, notes),
            env=_compose_env(svc.get("environment")),
            health=health
            or (
                HealthSpec(kind="http", path="/", port=ports[0].container)
                if ports and ports[0].container in WEB_PORTS
                else None
            ),
            cpus=float(limits.get("cpus", 1.0)),
            memory_mb=max(64, mem_mb),
            user=str(svc["user"]) if svc.get("user") is not None else None,
            read_only=bool(svc.get("read_only", False)),
            depends_on=[_slug(d) for d in (svc.get("depends_on") or [])],
        )
        services.append(service)
        for e in service.env:
            if e.secret:
                questions.append(
                    tr(
                        "Provide the secret {e_name} for {service_name}",
                        e_name=e.name,
                        service_name=service.name,
                    )
                )
    if any(p.exposure == Exposure.LAN for s in services for p in s.ports):
        questions.append(
            tr("Should the web interface be reachable only on the LAN or also publicly?")
        )
    return WorkloadSpec(
        name=_slug(name),
        services=services,
        notes=notes,
        questions=questions,
        confidence=0.85 if services else 0.1,
    )


def from_dockerfile(text: str, name: str) -> WorkloadSpec:
    ports = [
        PortSpec(
            container=int(p), exposure=Exposure.LAN if int(p) in WEB_PORTS else Exposure.INTERNAL
        )
        for p in re.findall(r"(?im)^EXPOSE\s+(\d+)", text)
    ]
    user = re.findall(r"(?im)^USER\s+(\S+)", text)
    volumes = [
        VolumeSpec(name=_slug(v.rsplit("/", 1)[-1] or "data"), path=v)
        for v in re.findall(r'(?im)^VOLUME\s+\[?"?([^"\]\s]+)', text)
    ]
    hc = re.search(
        r"(?im)^HEALTHCHECK .*?https?://(?:localhost|127\.0\.0\.1)(?::(\d+))?(/\S*)?", text
    )
    health = (
        HealthSpec(path=hc.group(2) or "/", port=int(hc.group(1)) if hc and hc.group(1) else None)
        if hc
        else (HealthSpec(path="/", port=ports[0].container) if ports else None)
    )
    notes = (
        []
        if user
        else [
            tr(
                "The image runs as root inside the container; it stays unprivileged on the host through user namespaces."
            )
        ]
    )
    svc = ServiceSpec(
        name=_slug(name),
        build_context=".",
        dockerfile="Dockerfile",
        ports=ports,
        volumes=volumes,
        health=health,
        user=user[-1] if user else None,
        read_only=False,
    )
    return WorkloadSpec(
        name=_slug(name),
        services=[svc],
        notes=notes,
        confidence=0.7,
        questions=[] if ports else [tr("Which port does the application listen on?")],
    )


def from_manifests(files: dict[str, str], name: str) -> WorkloadSpec | None:
    for manifest, pattern, port, path, note in FRAMEWORKS:
        if manifest in files and re.search(pattern, files[manifest]):
            svc = ServiceSpec(
                name=_slug(name),
                build_context=".",
                ports=[PortSpec(container=port, exposure=Exposure.LAN)],
                health=HealthSpec(path=path, port=port),
                read_only=False,
            )
            return WorkloadSpec(
                name=_slug(name),
                services=[svc],
                confidence=0.5,
                notes=[
                    tr(
                        "Detected a {note}. OpsTend generates a Containerfile for the build.",
                        note=note,
                    )
                ],
                questions=[tr("Check the start command and port of the application")],
            )
    return None


def analyze(
    files: dict[str, str], name: str, source: str | None = None, revision: str | None = None
) -> WorkloadSpec:
    """`files` maps repository paths to text for the small set of files we read."""
    compose = next(
        (
            files[p]
            for p in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
            if p in files
        ),
        None,
    )
    if compose:
        spec = from_compose(compose, name)
    elif "Containerfile" in files or "Dockerfile" in files:
        spec = from_dockerfile(files.get("Containerfile") or files["Dockerfile"], name)
    else:
        spec = from_manifests(files, name) or WorkloadSpec(
            name=_slug(name),
            services=[],
            confidence=0.0,
            questions=[
                tr("No container definition found. Which image or start command should run?")
            ],
        )
    example = files.get(".env.example") or files.get(".env.sample")
    if example and spec.services:
        known = {e.name for e in spec.services[0].env}
        extra = [e for e in parse_env_example(example) if e.name not in known]
        spec.services[0].env += extra
        spec.questions += [
            tr("Provide the secret {e_name}", e_name=e.name) for e in extra if e.secret
        ]
    spec.source, spec.revision = source, revision
    return spec


INTERESTING = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Containerfile",
    "Dockerfile",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    ".env.example",
    ".env.sample",
    "README.md",
)
