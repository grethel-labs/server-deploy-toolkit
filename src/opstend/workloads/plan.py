"""Where a workload can run, and the exact commands that run it isolated.

`candidates()` ranks hosts by free resources, architecture and requested devices
and explains every rejection. `podman_plan()` turns a workload into the argument
lists the host agent executes:

* one network per workload; `--internal` unless the egress policy allows more,
* an egress proxy with a host allowlist for `allowlist` egress,
* per-connection link networks that contain only the two endpoints,
* volumes, secrets and config files as Podman secrets (values via stdin only),
* hardened containers (`--cap-drop=ALL`, `no-new-privileges`, `--userns=auto`,
  limits, read-only root where possible) with only the approved devices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from opstend.core.models import Guest, Host, HostDevice, Platform
from opstend.deploy.spec import Exposure, ServiceSpec
from opstend.i18n import tr
from opstend.workloads.models import (
    DeviceKind,
    DeviceRequest,
    Egress,
    Workload,
)

EGRESS_PROXY_IMAGE = "docker.io/ubuntu/squid:6.6-24.04_edge"
INGRESS_IMAGE = "docker.io/alpine/socat:1.8.1.3"


# --- devices -----------------------------------------------------------------------------


def parse_udev(text: str) -> list[HostDevice]:
    """Devices from `udevadm info --export-db`."""
    out: list[HostDevice] = []
    for block in text.split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith("E: ") and "=" in line:
                key, _, value = line[3:].partition("=")
                props[key] = value
        dev, subsystem = props.get("DEVNAME", ""), props.get("SUBSYSTEM", "")
        if not dev:
            continue
        vendor = props.get("ID_VENDOR_ID")
        product = props.get("ID_MODEL_ID")
        name = " ".join(
            x
            for x in (props.get("ID_VENDOR_FROM_DATABASE"), props.get("ID_MODEL_FROM_DATABASE"))
            if x
        )
        if subsystem == "drm" and re.search(r"/renderD\d+$", dev):
            out.append(
                HostDevice(
                    id=dev,
                    kind="gpu",
                    name=name or "GPU render node",
                    path=dev,
                    driver=props.get("ID_PATH_TAG"),
                )
            )
        elif subsystem == "drm" and re.search(r"/card\d+$", dev):
            out.append(HostDevice(id=dev, kind="display", name=name or "Display output", path=dev))
        elif subsystem == "tty" and re.search(r"/tty(USB|ACM)\d+$", dev):
            out.append(
                HostDevice(
                    id=dev,
                    kind="serial",
                    name=name or dev.rsplit("/", 1)[-1],
                    path=dev,
                    vendor=vendor,
                    product=product,
                )
            )
        elif subsystem == "usb" and props.get("DEVTYPE") == "usb_device" and vendor and product:
            if props.get("ID_USB_INTERFACES", "").startswith(":09"):
                continue  # hubs
            out.append(
                HostDevice(
                    id=f"usb:{vendor}:{product}:{dev}",
                    kind="usb",
                    name=name or f"USB {vendor}:{product}",
                    path=dev,
                    vendor=vendor,
                    product=product,
                )
            )
        elif subsystem == "sound" and re.search(r"/snd/controlC\d+$", dev):
            out.append(HostDevice(id=dev, kind="audio", name=name or "Audio", path=dev))
    return out


def match_devices(
    requests: list[DeviceRequest], devices: list[HostDevice], workload_id: str
) -> tuple[list[HostDevice], list[str]]:
    """Devices for the requests, plus the reasons required ones cannot be met."""
    chosen: list[HostDevice] = []
    missing: list[str] = []
    for req in requests:
        kind = DeviceKind(req.kind)
        pool = [
            d
            for d in devices
            if d.kind == kind and d not in chosen and d.in_use_by in (None, workload_id)
        ]
        if req.selector:
            sel = req.selector.lower()
            pool = [
                d
                for d in pool
                if sel in f"{d.vendor}:{d.product}".lower()
                or sel in (d.path or "").lower()
                or sel in d.name.lower()
            ]
        if pool:
            chosen.append(pool[0])
        elif not req.optional:
            missing.append(tr("no free {kind} device", kind=kind.value))
    return chosen, missing


# --- placement ------------------------------------------------------------------------------


@dataclass
class Candidate:
    host_id: str
    name: str
    fits: bool
    reasons: list[str] = field(default_factory=list)
    free_cpu: float = 0
    free_memory_mb: int = 0
    devices: list[str] = field(default_factory=list)
    score: float = 0


def candidates(
    workload: Workload,
    hosts: list[Host],
    guests: list[Guest],
    reserved: dict[str, tuple[float, int]],
    architectures: list[str] | None = None,
) -> list[Candidate]:
    """Hosts that can run the workload as Podman containers, best first."""
    out: list[Candidate] = []
    for h in hosts:
        reasons: list[str] = []
        arch = next((t.split("=", 1)[1] for t in h.tags if t.startswith("arch=")), "amd64")
        used_cpu, used_mem = reserved.get(h.id, (0.0, 0))
        free_cpu = h.capacity.cpu_cores * 0.9 - used_cpu
        free_mem = int(h.capacity.memory_mb * 0.85) - (h.usage.memory_mb or 0) - used_mem
        if Platform(h.platform) != Platform.LINUX:
            reasons.append(tr("containers run on Linux hosts"))
        if architectures and arch not in architectures:
            reasons.append(tr("the images do not support {arch}", arch=arch))
        if free_cpu < workload.resources.cpus:
            reasons.append(tr("only {free:.1f} CPU free", free=max(free_cpu, 0)))
        if free_mem < workload.resources.memory_mb:
            reasons.append(tr("only {free} MB memory free", free=max(free_mem, 0)))
        if h.health in ("critical",):
            reasons.append(tr("the host is not healthy"))
        chosen, missing = match_devices(workload.devices, h.devices, workload.id)
        reasons += missing
        fits = not reasons
        score = (free_mem / max(workload.resources.memory_mb, 1)) + free_cpu + len(chosen) * 10
        out.append(
            Candidate(
                h.id,
                h.name,
                fits,
                reasons,
                round(free_cpu, 1),
                free_mem,
                [d.name for d in chosen],
                score if fits else -1,
            )
        )
    return sorted(out, key=lambda c: c.score, reverse=True)


def reservations(workloads: list[Workload]) -> dict[str, tuple[float, int]]:
    out: dict[str, tuple[float, int]] = {}
    for w in workloads:
        if w.host_id and w.state not in ("removed", "expired", "draft"):
            cpu, mem = out.get(w.host_id, (0.0, 0))
            out[w.host_id] = (cpu + w.resources.cpus, mem + w.resources.memory_mb)
    return out


# --- podman -----------------------------------------------------------------------------------


@dataclass
class Command:
    argv: list[str]
    describe: str
    stdin_secret: str | None = None  # secret id whose value goes to stdin
    stdin_text: str | None = None  # non-secret stdin (config files)
    undo: list[str] | None = None
    check: bool = True


def _squid_config(hosts: list[str]) -> str:
    acl = " ".join(("." + h[2:]) if h.startswith("*.") else h for h in hosts) or "none.invalid"
    return "\n".join(
        [
            "http_port 3128",
            f"acl allowed dstdomain {acl}",
            "acl SSL_ports port 443",
            "acl CONNECT method CONNECT",
            "http_access deny CONNECT !SSL_ports",
            "http_access allow allowed",
            "http_access deny all",
            "cache deny all",
            "access_log stdio:/dev/stdout",
            "",
        ]
    )


def _device_args(devices: list[HostDevice]) -> list[str]:
    args: list[str] = []
    for d in devices:
        if d.kind == "gpu" and d.path and d.path.startswith("nvidia"):
            args += ["--device", "nvidia.com/gpu=all"]
        elif d.path:
            args += ["--device", f"{d.path}:{d.path}:rwm"]
    if any(d.kind in ("gpu", "display") for d in devices):
        args += ["--group-add", "keep-groups"]
    return args


def service_args(
    w: Workload,
    s: ServiceSpec,
    bind: str,
    devices: list[HostDevice],
    proxy: bool,
    publish: bool = True,
) -> list[str]:
    name = w.container(s.name)
    args = [
        "podman",
        "run",
        "--detach",
        "--replace",
        "--name",
        name,
        "--label",
        f"opstend.workload={w.id}",
        "--label",
        f"opstend.service={s.name}",
        "--label",
        f"opstend.environment={w.environment}",
        "--network",
        f"{w.network_name}:alias={s.name}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--userns=auto",
        f"--cpus={s.cpus}",
        f"--memory={s.memory_mb}m",
        "--pids-limit=1024",
        "--restart=on-failure:5",
        "--log-opt",
        "max-size=10m",
    ]
    if s.read_only:
        args += ["--read-only", "--tmpfs=/tmp:rw,size=64m"]
    for v in s.volumes:
        args += ["--volume", f"opstend-{w.id}-{v.name}:{v.path}:Z"]
    for e in s.env:
        if e.secret and e.name in w.secrets:
            args += ["--secret", f"opstend-{w.id}-{e.name.lower()},type=env,target={e.name}"]
        elif not e.secret and e.value is not None:
            args += ["--env", f"{e.name}={e.value}"]
    for i, f in enumerate(x for x in w.files if x["service"] == s.name):
        args += [
            "--secret",
            f"opstend-{w.id}-file-{s.name}-{i},type=mount,target={f['path']},mode=0444",
        ]
    if proxy:
        proxy_url = "http://egress:3128"
        args += [
            "--env",
            f"HTTP_PROXY={proxy_url}",
            "--env",
            f"HTTPS_PROXY={proxy_url}",
            "--env",
            f"http_proxy={proxy_url}",
            "--env",
            f"https_proxy={proxy_url}",
            "--env",
            "NO_PROXY=localhost,127.0.0.1",
        ]
    if publish:
        for p in s.ports:
            if p.exposure in (Exposure.LAN, Exposure.PUBLIC):
                args += ["--publish", f"{bind}:{p.container}:{p.container}/{p.protocol}"]
    args += _device_args(devices)
    if s.health and s.health.kind == "http":
        port = s.health.port or (s.ports[0].container if s.ports else 80)
        args += [
            "--health-cmd",
            f"wget -q -O /dev/null http://127.0.0.1:{port}{s.health.path}",
            "--health-interval",
            "30s",
        ]
    args.append(s.image or f"localhost/opstend-{w.id}-{s.name}:latest")
    if s.command:
        args += s.command
    return args


def startup_order(services: list[ServiceSpec]) -> list[ServiceSpec]:
    done: list[ServiceSpec] = []
    pending = list(services)
    while pending:
        ready = [s for s in pending if all(d in {x.name for x in done} for d in s.depends_on)]
        if not ready:
            raise ValueError("services depend on each other in a cycle")
        done += ready
        pending = [s for s in pending if s not in ready]
    return done


def podman_plan(
    w: Workload, bind: str, devices: list[HostDevice], peers: dict[str, Workload] | None = None
) -> list[Command]:
    """Commands that create the workload. Each command lists how to undo it."""
    peers = peers or {}
    plan: list[Command] = []
    internal = Egress(w.network.egress) != Egress.INTERNET
    net = ["podman", "network", "create", "--label", f"opstend.workload={w.id}"]
    # Internal networks have no route out; bridge networks are isolated from each other.
    net += ["--internal"] if internal else ["--opt", "isolate=true"]
    plan.append(
        Command(
            [*net, w.network_name],
            tr("Create the isolated network"),
            undo=["podman", "network", "rm", "-f", w.network_name],
            check=False,
        )
    )
    proxy = Egress(w.network.egress) == Egress.ALLOWLIST
    if proxy:
        cfg = f"opstend-{w.id}-egress-config"
        plan.append(
            Command(
                ["podman", "secret", "create", "--replace", cfg, "-"],
                tr("Store the outbound allowlist"),
                stdin_text=_squid_config(w.network.egress_hosts),
                undo=["podman", "secret", "rm", cfg],
                check=True,
            )
        )
        plan.append(
            Command(
                [
                    "podman",
                    "run",
                    "--detach",
                    "--replace",
                    "--name",
                    f"{w.id}-egress",
                    "--label",
                    f"opstend.workload={w.id}",
                    "--network",
                    f"{w.network_name}:alias=egress",
                    "--cap-drop=ALL",
                    "--cap-add=SETUID,SETGID",
                    "--security-opt=no-new-privileges",
                    "--memory=128m",
                    "--secret",
                    f"{cfg},type=mount,target=/etc/squid/squid.conf,mode=0444",
                    EGRESS_PROXY_IMAGE,
                ],
                tr(
                    "Start the outbound proxy for {count} allowed hosts",
                    count=len(w.network.egress_hosts),
                ),
                undo=["podman", "rm", "-f", f"{w.id}-egress"],
            )
        )
        plan.append(
            Command(
                ["podman", "network", "connect", "podman", f"{w.id}-egress"],
                tr("Let only the proxy reach the internet"),
                check=False,
            )
        )
    for s in w.services:
        for v in s.volumes:
            vol = f"opstend-{w.id}-{v.name}"
            plan.append(
                Command(
                    [
                        "podman",
                        "volume",
                        "create",
                        "--ignore",
                        "--label",
                        f"opstend.workload={w.id}",
                        vol,
                    ],
                    tr("Create data volume {name}", name=v.name),
                )
            )
    for env, secret_id in sorted(w.secrets.items()):
        name = f"opstend-{w.id}-{env.lower()}"
        plan.append(
            Command(
                ["podman", "secret", "create", "--replace", name, "-"],
                tr("Store secret {name}", name=env),
                stdin_secret=secret_id,
                undo=["podman", "secret", "rm", name],
            )
        )
    for s in w.services:
        for i, f in enumerate(x for x in w.files if x["service"] == s.name):
            name = f"opstend-{w.id}-file-{s.name}-{i}"
            plan.append(
                Command(
                    ["podman", "secret", "create", "--replace", name, "-"],
                    tr("Write {path}", path=f["path"]),
                    stdin_text=f["content"],
                    undo=["podman", "secret", "rm", name],
                )
            )
    for s in startup_order(w.services):
        if s.image:
            plan.append(
                Command(
                    ["podman", "pull", "--quiet", s.image], tr("Download {image}", image=s.image)
                )
            )
        plan.append(
            Command(
                service_args(w, s, bind, devices, proxy, publish=not internal),
                tr("Start {service}", service=s.name),
                undo=["podman", "rm", "-f", w.container(s.name)],
            )
        )
    if internal:
        # Internal networks cannot publish ports: a minimal forwarder in its own isolated
        # network is the only thing reachable from outside.
        ingress = [
            (s, p)
            for s in w.services
            for p in s.ports
            if p.exposure in (Exposure.LAN, Exposure.PUBLIC) and p.protocol == "tcp"
        ]
        if ingress:
            in_net = f"{w.network_name}-in"
            plan.append(
                Command(
                    [
                        "podman",
                        "network",
                        "create",
                        "--label",
                        f"opstend.workload={w.id}",
                        "--opt",
                        "isolate=true",
                        in_net,
                    ],
                    tr("Create the entry network"),
                    undo=["podman", "network", "rm", "-f", in_net],
                    check=False,
                )
            )
        for s, p in ingress:
            name = f"{w.id}-in-{p.container}"
            plan.append(
                Command(
                    [
                        "podman",
                        "run",
                        "--detach",
                        "--replace",
                        "--name",
                        name,
                        "--label",
                        f"opstend.workload={w.id}",
                        "--network",
                        f"{w.network_name}-in",
                        "--cap-drop=ALL",
                        "--security-opt=no-new-privileges",
                        "--read-only",
                        "--memory=32m",
                        "--publish",
                        f"{bind}:{p.container}:{p.container}/tcp",
                        INGRESS_IMAGE,
                        f"TCP-LISTEN:{p.container},fork,reuseaddr",
                        f"TCP:{s.name}:{p.container}",
                    ],
                    tr("Open port {port} for {service}", port=p.container, service=s.name),
                    undo=["podman", "rm", "-f", name],
                )
            )
            plan.append(
                Command(
                    ["podman", "network", "connect", w.network_name, name],
                    tr("Connect the entry to {service}", service=s.name),
                    check=False,
                )
            )
    for c in w.connections:
        link = f"opstend-link-{w.id}-{c.to_workload}"
        plan.append(
            Command(
                [
                    "podman",
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    f"opstend.workload={w.id}",
                    link,
                ],
                tr("Create the path to {target}", target=f"{c.to_workload}/{c.to_service}"),
                undo=["podman", "network", "rm", "-f", link],
                check=False,
            )
        )
        target = peers.get(c.to_workload)
        target_container = (
            target.container(c.to_service) if target else f"{c.to_workload}-{c.to_service}"
        )
        plan.append(
            Command(
                [
                    "podman",
                    "network",
                    "connect",
                    "--alias",
                    f"{c.to_workload}-{c.to_service}",
                    link,
                    target_container,
                ],
                tr("Attach {target} to the path", target=c.to_service),
                check=False,
            )
        )
        for s in w.services:
            plan.append(
                Command(
                    ["podman", "network", "connect", link, w.container(s.name)],
                    tr("Attach {service} to the path", service=s.name),
                    check=False,
                )
            )
    return plan


def removal_plan(w: Workload, purge_data: bool = False) -> list[Command]:
    plan = [
        Command(
            ["podman", "rm", "-f", "--depend", w.container(s.name)],
            tr("Remove {service}", service=s.name),
            check=False,
        )
        for s in w.services
    ]
    plan.append(
        Command(
            ["podman", "rm", "-f", f"{w.id}-egress"], tr("Remove the outbound proxy"), check=False
        )
    )
    for s in w.services:
        for p in s.ports:
            plan.append(
                Command(
                    ["podman", "rm", "-f", f"{w.id}-in-{p.container}"],
                    tr("Close port {port}", port=p.container),
                    check=False,
                )
            )
    plan.append(
        Command(
            ["podman", "network", "rm", "-f", f"{w.network_name}-in"],
            tr("Remove the entry network"),
            check=False,
        )
    )
    for c in w.connections:
        plan.append(
            Command(
                ["podman", "network", "rm", "-f", f"opstend-link-{w.id}-{c.to_workload}"],
                tr("Remove the path to {target}", target=c.to_workload),
                check=False,
            )
        )
    plan.append(
        Command(
            ["podman", "network", "rm", "-f", w.network_name],
            tr("Remove the isolated network"),
            check=False,
        )
    )
    for env in w.secrets:
        plan.append(
            Command(
                ["podman", "secret", "rm", f"opstend-{w.id}-{env.lower()}"],
                tr("Remove secret {name}", name=env),
                check=False,
            )
        )
    if purge_data:
        for s in w.services:
            for v in s.volumes:
                plan.append(
                    Command(
                        ["podman", "volume", "rm", "-f", f"opstend-{w.id}-{v.name}"],
                        tr("Delete data volume {name}", name=v.name),
                        check=False,
                    )
                )
    return plan


def isolation_summary(w: Workload) -> list[dict[str, Any]]:
    """Plain statements the UI shows before deployment."""
    egress = Egress(w.network.egress)
    lines = [
        {"ok": True, "text": tr("Runs in its own network; other workloads cannot reach it.")},
        {
            "ok": egress == Egress.NONE,
            "text": tr("No internet access.")
            if egress == Egress.NONE
            else (
                tr("Internet only for: {hosts}", hosts=", ".join(w.network.egress_hosts))
                if egress == Egress.ALLOWLIST
                else tr("Full internet access.")
            ),
        },
        {
            "ok": w.exposure() != Exposure.PUBLIC,
            "text": {
                Exposure.INTERNAL: tr("Reachable only by OpsTend."),
                Exposure.LAN: tr("Reachable from the local network."),
                Exposure.PUBLIC: tr("Reachable from the internet through HTTPS."),
            }[w.exposure()],
        },
        {
            "ok": True,
            "text": tr(
                "Containers run without capabilities, with their own user namespace and resource limits."
            ),
        },
    ]
    for c in w.connections:
        lines.append(
            {
                "ok": True,
                "text": tr(
                    "May reach {target} on port {port}: {reason}",
                    target=f"{c.to_workload}/{c.to_service}",
                    port=c.port,
                    reason=c.reason or "–",
                ),
            }
        )
    for d in w.devices:
        lines.append(
            {
                "ok": False,
                "text": tr(
                    "Gets direct access to a {kind} device: {reason}",
                    kind=d.kind,
                    reason=d.reason or "–",
                ),
            }
        )
    if w.expires_at:
        lines.append(
            {
                "ok": True,
                "text": tr(
                    "Sandbox: removed automatically at {time}",
                    time=w.expires_at.strftime("%Y-%m-%d %H:%M"),
                ),
            }
        )
    return lines
