"""Any Linux server on any architecture.

The adapter detects the distribution, CPU architecture, package manager and
init system, then offers the same functions as Proxmox where the host can
provide them: Podman containers and libvirt VMs with power actions, snapshots
(libvirt), backups, clones and live migration (libvirt), plus package
inventory and updates for apt, dnf/yum, zypper, apk and pacman.
"""

from __future__ import annotations

import json
import platform as pyplatform
import re
from dataclasses import dataclass, field
from typing import Any

from opstend.adapters.base import Bridge, Capability, GuestAction, Inventory, Unsupported
from opstend.adapters.runner import CommandError, Runner
from opstend.core.models import (
    Guest,
    GuestKind,
    GuestState,
    Health,
    Host,
    Nic,
    Observed,
    Platform,
    Resources,
    Snapshot,
    Update,
    Usage,
)

ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "armv7",
    "armv7": "armv7",
    "armv6l": "armv6",
    "i686": "386",
    "i386": "386",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
    "riscv64": "riscv64",
    "loongarch64": "loong64",
}


def normalize_arch(machine: str) -> str:
    return ARCH_ALIASES.get(machine.strip().lower(), machine.strip().lower())


def parse_os_release(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def package_manager_for(os_release: dict[str, str]) -> str:
    ids = {os_release.get("ID", "")} | set(os_release.get("ID_LIKE", "").split())
    if ids & {"debian", "ubuntu", "raspbian", "linuxmint", "pop"}:
        return "apt"
    if ids & {"alpine"}:
        return "apk"
    if ids & {"arch", "manjaro", "endeavouros"}:
        return "pacman"
    if ids & {"opensuse", "suse", "sles", "opensuse-leap", "opensuse-tumbleweed"}:
        return "zypper"
    if ids & {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn"}:
        return "dnf"
    return "unknown"


# --- update parsers ---------------------------------------------------------


def parse_apt_simulate(text: str) -> list[dict[str, Any]]:
    """`apt-get -s upgrade`: `Inst pkg [old] (new Origin:release [arch])`."""
    out = []
    for m in re.finditer(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+) ([^\[)]*)", text, re.M):
        name, old, new, origin = m.groups()
        out.append(
            {
                "name": name,
                "current": old or "",
                "available": new,
                "security": "security" in origin.lower(),
            }
        )
    return out


def parse_dnf_check_update(text: str, security: set[str]) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) != 3
            or "." not in parts[0]
            or line.startswith(("Last metadata", "Obsoleting"))
        ):
            continue
        name = parts[0].rsplit(".", 1)[0]
        out.append(
            {"name": name, "current": "", "available": parts[1], "security": name in security}
        )
    return out


def parse_dnf_security(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "/Sec" in parts[1]:
            names.add(re.sub(r"-[^-]+-[^-]+$", "", parts[2]))
    return names


def parse_zypper_lu(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 6 and cols[0] == "v":
            out.append(
                {"name": cols[2], "current": cols[3], "available": cols[4], "security": False}
            )
    return out


def parse_apk_upgradable(text: str) -> list[dict[str, Any]]:
    out = []
    for m in re.finditer(
        r"^(\S+?)-(\d\S*-r\d+) .*\[upgradable from: \S+?-(\d\S*-r\d+)\]", text, re.M
    ):
        out.append(
            {"name": m.group(1), "current": m.group(3), "available": m.group(2), "security": False}
        )
    return out


def parse_checkupdates(text: str) -> list[dict[str, Any]]:
    out = []
    for m in re.finditer(r"^(\S+) (\S+) -> (\S+)$", text, re.M):
        out.append(
            {"name": m.group(1), "current": m.group(2), "available": m.group(3), "security": False}
        )
    return out


# --- installed packages (security scans) --------------------------------------

INSTALLED = {
    "apt": ["dpkg-query", "-W", "-f", "${Package}\t${Version}\t${Architecture}\n"],
    "dnf": ["rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n"],
    "zypper": ["rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n"],
    "apk": ["apk", "info", "-v"],
    "pacman": ["pacman", "-Q"],
}

ECOSYSTEM = {"apt": "Debian", "dnf": "Red Hat", "zypper": "SUSE", "apk": "Alpine", "pacman": "Arch"}


def parse_installed(manager: str, text: str) -> list[tuple[str, str]]:
    pkgs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if manager in ("apt", "dnf", "zypper"):
            parts = line.split("\t")
            if len(parts) >= 2:
                version = parts[1][2:] if parts[1].startswith("0:") else parts[1]
                pkgs.append((parts[0], version))
        elif manager == "apk":
            m = re.match(r"^(.+?)-(\d[^-]*-r\d+)$", line.strip())
            if m:
                pkgs.append((m.group(1), m.group(2)))
        elif manager == "pacman":
            parts = line.split()
            if len(parts) == 2:
                pkgs.append((parts[0], parts[1]))
    return pkgs


UPGRADE = {
    "apt": [
        ["apt-get", "update", "-q"],
        ["apt-get", "-y", "-q", "-o", "Dpkg::Options::=--force-confold", "upgrade"],
    ],
    "dnf": [["dnf", "-y", "-q", "upgrade"]],
    "zypper": [["zypper", "-n", "-q", "update"]],
    "apk": [["apk", "update", "-q"], ["apk", "upgrade", "-q"]],
    "pacman": [["pacman", "-Syu", "--noconfirm"]],
}
SECURITY_ONLY = {
    "dnf": [["dnf", "-y", "-q", "upgrade", "--security"]],
    "zypper": [["zypper", "-n", "-q", "patch", "--category", "security"]],
}


@dataclass
class HostFacts:
    hostname: str = ""
    os: dict[str, str] = field(default_factory=dict)
    arch: str = ""
    kernel: str = ""
    manager: str = "unknown"
    init: str = "unknown"
    cpus: int = 0
    memory_mb: int = 0
    podman: bool = False
    libvirt: bool = False


def parse_meminfo(text: str) -> tuple[int, int]:
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            values[key] = int(rest.split()[0])
    total = values.get("MemTotal", 0) // 1024
    available = values.get("MemAvailable", values.get("MemFree", 0)) // 1024
    return total, total - available


def parse_podman_ps(text: str, host_id: str) -> list[Guest]:
    guests = []
    for c in json.loads(text or "[]"):
        name = (c.get("Names") or [c.get("Id", "")[:12]])[0]
        state = str(c.get("State", "")).lower()
        guests.append(
            Guest(
                id=f"{host_id}:podman:{name}",
                host_id=host_id,
                kind=GuestKind.CONTAINER,
                native_id=name,
                name=name,
                image=c.get("Image"),
                state=GuestState.RUNNING
                if state == "running"
                else (GuestState.PAUSED if state == "paused" else GuestState.STOPPED),
                nics=[Nic(name=n, ips=[]) for n in (c.get("Networks") or [])],
                tags=[
                    f"{k}={v}"
                    for k, v in sorted((c.get("Labels") or {}).items())
                    if k.startswith("opstend.")
                ],
                observed=Observed(source="podman"),
            )
        )
    return guests


def parse_virsh_list(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines()[2:]:
        parts = line.split(None, 2)
        if len(parts) == 3:
            out.append((parts[1], parts[2].strip()))
    return out


def parse_virsh_domiflist(text: str) -> list[Nic]:
    nics = []
    for line in text.splitlines()[2:]:
        parts = line.split()
        if len(parts) == 5:
            nics.append(
                Nic(name=parts[0], bridge=parts[2] if parts[2] != "-" else None, mac=parts[4])
            )
    return nics


class Linux:
    name = "linux"

    def __init__(
        self, host_id: str, address: str, runner: Runner | None = None, site: str = "default"
    ) -> None:
        self.host_id = host_id
        self.address = address
        self.runner = runner or Runner()
        self.site = site
        self.facts = HostFacts()

    async def _out(self, argv: list[str], check: bool = True) -> str:
        return (await self.runner.run(argv, check=check)).stdout

    async def detect(self) -> HostFacts:
        f = HostFacts()
        f.os = parse_os_release(await self._out(["cat", "/etc/os-release"]))
        uname = (await self._out(["uname", "-n", "-m", "-r"])).split()
        f.hostname, f.kernel, f.arch = uname[0], uname[1], normalize_arch(uname[2])
        f.manager = package_manager_for(f.os)
        f.cpus = int((await self._out(["nproc"])).strip() or 0)
        f.memory_mb, _ = parse_meminfo(await self._out(["cat", "/proc/meminfo"]))
        comm = (await self._out(["cat", "/proc/1/comm"], check=False)).strip()
        f.init = (
            "systemd"
            if comm == "systemd"
            else ("openrc" if comm in ("init", "openrc-init") else comm or "unknown")
        )
        f.podman = (await self.runner.run(["podman", "--version"], check=False)).code == 0
        f.libvirt = (await self.runner.run(["virsh", "--version"], check=False)).code == 0
        self.facts = f
        return f

    async def inventory(self) -> Inventory:
        inv = Inventory()
        try:
            f = await self.detect()
        except (CommandError, OSError) as exc:
            inv.hosts.append(
                Host(
                    id=self.host_id,
                    name=self.address,
                    platform=Platform.LINUX,
                    address=self.address,
                    site=self.site,
                    health=Health.CRITICAL,
                )
            )
            inv.errors.append(f"{self.address}: {exc}")
            return inv
        total, used = parse_meminfo(await self._out(["cat", "/proc/meminfo"]))
        load = (await self._out(["cat", "/proc/loadavg"])).split()
        cpu_pct = min(100.0, float(load[0]) / max(1, f.cpus) * 100) if load else None
        inv.hosts.append(
            Host(
                id=self.host_id,
                name=f.hostname,
                platform=Platform.LINUX,
                address=self.address,
                site=self.site,
                os=f"{f.os.get('PRETTY_NAME', 'Linux')} ({f.arch})",
                version=f.kernel,
                health=Health.OK,
                capacity=Resources(cpu_cores=f.cpus, memory_mb=total),
                usage=Usage(
                    cpu_pct=round(cpu_pct, 1) if cpu_pct is not None else None, memory_mb=used
                ),
                observed=Observed(source="linux-agent"),
                tags=[f"arch={f.arch}", f"pkg={f.manager}", f"init={f.init}"],
            )
        )
        udev = await self.runner.run(["udevadm", "info", "--export-db"], check=False)
        if udev.code == 0:
            from opstend.workloads.plan import parse_udev

            inv.hosts[-1].devices = parse_udev(udev.stdout)
        if f.podman:
            inv.hosts[-1].tags.append("podman")
        inv.bridges += await self._bridges()
        if f.podman:
            inv.guests += parse_podman_ps(
                await self._out(["podman", "ps", "-a", "--format", "json"]), self.host_id
            )
        if f.libvirt:
            for name, _state in parse_virsh_list(await self._out(["virsh", "list", "--all"])):
                inv.guests.append(await self._vm(name))
        for upd in await self.updates():
            inv.updates.append(
                Update(
                    id=f"{self.host_id}:{upd['name']}",
                    target=self.host_id,
                    kind="package",
                    current=upd["current"],
                    available=upd["available"],
                    security=upd["security"],
                )
            )
        return inv

    async def _bridges(self) -> list[Bridge]:
        try:
            links = json.loads(await self._out(["ip", "-j", "-d", "link", "show"]))
            addrs = json.loads(await self._out(["ip", "-j", "addr", "show"]))
        except (CommandError, ValueError):
            return []
        cidr = {
            a["ifname"]: next(
                (
                    f"{i['local']}/{i['prefixlen']}"
                    for i in a.get("addr_info", [])
                    if i.get("family") == "inet"
                ),
                None,
            )
            for a in addrs
        }
        bridges = {
            link["ifname"]: Bridge(
                host_id=self.host_id,
                name=link["ifname"],
                address=cidr.get(link["ifname"]),
                mac=link.get("address"),
                vlan_aware=bool(
                    link.get("linkinfo", {}).get("info_data", {}).get("vlan_filtering")
                ),
            )
            for link in links
            if link.get("linkinfo", {}).get("info_kind") == "bridge"
        }
        for link in links:
            master = link.get("master")
            if master in bridges and link.get("linkinfo", {}).get("info_kind") in (
                None,
                "vlan",
                "bond",
            ):
                bridges[master].ports.append(link["ifname"])
        return list(bridges.values())

    async def _vm(self, name: str) -> Guest:
        info = await self._out(["virsh", "dominfo", name])
        fields = {
            k.strip(): v.strip() for k, _, v in (ln.partition(":") for ln in info.splitlines())
        }
        state = fields.get("State", "")
        snaps = [
            Snapshot(name=s)
            for s in (
                await self._out(["virsh", "snapshot-list", "--name", name], check=False)
            ).split()
        ]
        return Guest(
            id=f"{self.host_id}:libvirt:{name}",
            host_id=self.host_id,
            kind=GuestKind.VM,
            native_id=name,
            name=name,
            state=GuestState.RUNNING
            if state == "running"
            else (GuestState.PAUSED if state == "paused" else GuestState.STOPPED),
            capacity=Resources(
                cpu_cores=float(fields.get("CPU(s)", 0) or 0),
                memory_mb=int(fields.get("Max memory", "0 KiB").split()[0]) // 1024,
            ),
            nics=parse_virsh_domiflist(await self._out(["virsh", "domiflist", name], check=False)),
            snapshots=snaps,
            observed=Observed(source="libvirt"),
        )

    # -- packages --------------------------------------------------------

    async def updates(self) -> list[dict[str, Any]]:
        m = self.facts.manager
        if m == "apt":
            return parse_apt_simulate(await self._out(["apt-get", "-s", "-q", "upgrade"]))
        if m == "dnf":
            sec = parse_dnf_security(
                await self._out(["dnf", "-q", "updateinfo", "list", "--security"], check=False)
            )
            res = await self.runner.run(["dnf", "-q", "check-update"], check=False)
            return parse_dnf_check_update(res.stdout, sec)
        if m == "zypper":
            return parse_zypper_lu(await self._out(["zypper", "-q", "list-updates"]))
        if m == "apk":
            return parse_apk_upgradable(await self._out(["apk", "-u", "list"]))
        if m == "pacman":
            return parse_checkupdates(await self._out(["checkupdates"], check=False))
        return []

    async def installed(self) -> tuple[str, list[tuple[str, str]]]:
        argv = INSTALLED.get(self.facts.manager)
        if not argv:
            return ("", [])
        return (
            ECOSYSTEM[self.facts.manager],
            parse_installed(self.facts.manager, await self._out(argv)),
        )

    async def upgrade(self, security_only: bool = False) -> None:
        m = self.facts.manager
        steps = SECURITY_ONLY.get(m) if security_only and m in SECURITY_ONLY else UPGRADE.get(m)
        if not steps:
            raise Unsupported(f"Linux ({m})", "package upgrades")
        env = {"DEBIAN_FRONTEND": "noninteractive"} if m == "apt" else {}
        for argv in steps:
            await self.runner.run(argv, timeout=3600, env=env)

    async def reboot_required(self) -> bool:
        m = self.facts.manager
        if m == "apt":
            return (
                await self.runner.run(["cat", "/var/run/reboot-required"], check=False)
            ).code == 0
        if m in ("dnf",):
            return (await self.runner.run(["needs-restarting", "-r"], check=False)).code == 1
        if m == "zypper":
            return (await self.runner.run(["zypper", "needs-rebooting"], check=False)).code == 102
        return False

    # -- guests ----------------------------------------------------------

    def capabilities(self, guest: Guest) -> set[Capability]:
        if guest.kind == GuestKind.VM:
            return {
                Capability.POWER,
                Capability.SNAPSHOT,
                Capability.BACKUP,
                Capability.CLONE,
                Capability.MIGRATE,
            }
        return {Capability.POWER, Capability.BACKUP, Capability.CLONE}

    async def power(self, guest: Guest, action: GuestAction) -> None:
        if guest.kind == GuestKind.CONTAINER:
            verb = {
                GuestAction.START: "start",
                GuestAction.STOP: "kill",
                GuestAction.SHUTDOWN: "stop",
                GuestAction.REBOOT: "restart",
                GuestAction.SUSPEND: "pause",
                GuestAction.RESUME: "unpause",
            }[GuestAction(action)]
            await self.runner.run(["podman", verb, guest.native_id])
            return
        verb = {
            GuestAction.START: "start",
            GuestAction.STOP: "destroy",
            GuestAction.SHUTDOWN: "shutdown",
            GuestAction.REBOOT: "reboot",
            GuestAction.SUSPEND: "suspend",
            GuestAction.RESUME: "resume",
        }[GuestAction(action)]
        await self.runner.run(["virsh", verb, guest.native_id])

    async def snapshot(self, guest: Guest, name: str, description: str = "") -> None:
        if guest.kind != GuestKind.VM:
            raise Unsupported("Podman", "snapshots")
        await self.runner.run(
            [
                "virsh",
                "snapshot-create-as",
                guest.native_id,
                name,
                "--description",
                description or "OpsTend snapshot",
                "--atomic",
            ]
        )

    async def delete_snapshot(self, guest: Guest, name: str) -> None:
        if guest.kind != GuestKind.VM:
            raise Unsupported("Podman", "snapshots")
        await self.runner.run(["virsh", "snapshot-delete", guest.native_id, name])

    async def rollback_snapshot(self, guest: Guest, name: str) -> None:
        if guest.kind != GuestKind.VM:
            raise Unsupported("Podman", "snapshots")
        await self.runner.run(["virsh", "snapshot-revert", guest.native_id, name])

    async def backup(self, guest: Guest, destination: str) -> str:
        """Back up into a restic repository. Credentials come from the agent's environment."""
        tag = f"opstend-guest={guest.id}"
        if guest.kind == GuestKind.CONTAINER:
            mounts = json.loads(
                await self._out(
                    ["podman", "inspect", "--format", "{{json .Mounts}}", guest.native_id]
                )
                or "[]"
            )
            paths = [m["Source"] for m in mounts if m.get("Source")]
            if not paths:
                return "nothing to back up: the container has no volumes"
            res = await self.runner.run(
                ["restic", "-r", destination, "backup", "--tag", tag, "--json", *paths],
                timeout=6 * 3600,
            )
        else:
            disks = await self._out(["virsh", "domblklist", guest.native_id, "--details"])
            paths = [
                ln.split()[3]
                for ln in disks.splitlines()[2:]
                if len(ln.split()) == 4 and ln.split()[1] == "disk"
            ]
            res = await self.runner.run(
                ["restic", "-r", destination, "backup", "--tag", tag, "--json", *paths],
                timeout=6 * 3600,
            )
        return res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""

    async def clone(self, template: Guest, name: str, target_host: str | None = None) -> str:
        if target_host and target_host != self.host_id:
            raise Unsupported("Linux", "cloning to another host")
        if template.kind == GuestKind.CONTAINER:
            await self.runner.run(["podman", "container", "clone", template.native_id, name])
        else:
            await self.runner.run(
                ["virt-clone", "--original", template.native_id, "--name", name, "--auto-clone"],
                timeout=3600,
            )
        return name

    async def migrate(self, guest: Guest, target_host: str, online: bool = True) -> None:
        if guest.kind != GuestKind.VM:
            raise Unsupported("Podman", "live migration; redeploy the service instead")
        flags = (
            ["--live", "--persistent", "--undefinesource"]
            if online
            else ["--offline", "--persistent", "--undefinesource"]
        )
        await self.runner.run(
            ["virsh", "migrate", *flags, guest.native_id, f"qemu+ssh://{target_host}/system"],
            timeout=3600,
        )

    def console_url(self, guest: Guest) -> str | None:
        return None


def local_arch() -> str:
    return normalize_arch(pyplatform.machine())
