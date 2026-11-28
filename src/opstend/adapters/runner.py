"""Run local programs safely.

Commands are argument lists, never shell strings. Only programs on the
allowlist can run, with a timeout and a bounded output size. Remote Linux
hosts use the same runner through SSH with a pinned host key.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from dataclasses import dataclass, field

ALLOWED = {
    # inventory
    "uname",
    "cat",
    "hostnamectl",
    "ip",
    "lsblk",
    "df",
    "nproc",
    "free",
    "lscpu",
    # packages
    "apt-get",
    "apt",
    "dpkg-query",
    "dnf",
    "yum",
    "rpm",
    "zypper",
    "apk",
    "pacman",
    "needs-restarting",
    "checkupdates",
    # services and init
    "systemctl",
    "rc-service",
    "rc-update",
    "journalctl",
    "loginctl",
    # workloads
    "podman",
    "docker",
    "virsh",
    "qm",
    "pct",
    "pvesh",
    "pvecm",
    # security and backup
    "trivy",
    "restic",
    "proxmox-backup-client",
    "nft",
    "sshd",
    "ss",
    # network
    "wg",
    "udevadm",
    "nvidia-smi",
    "bridge",
    "ethtool",
    "lldpctl",
    "virt-clone",
    "env",
    # self
    "opstend",
}


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], code: int, stderr: str) -> None:
        super().__init__(f"{argv[0]} exited with {code}: {stderr.strip()[:300]}")
        self.argv = argv
        self.code = code
        self.stderr = stderr


@dataclass
class Result:
    argv: list[str]
    code: int
    stdout: str
    stderr: str


@dataclass
class Runner:
    """Local runner. Set `ssh` to run on a remote host instead."""

    ssh: list[str] | None = None
    timeout: float = 120.0
    max_output: int = 8 * 1024 * 1024
    env: dict[str, str] = field(default_factory=lambda: {"LC_ALL": "C", "LANG": "C"})

    def available(self, program: str) -> bool:
        return self.ssh is not None or shutil.which(program) is not None

    async def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        input_data: bytes | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Result:
        if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        program = os.path.basename(argv[0])
        if program not in ALLOWED:
            raise PermissionError(f"{program} is not on the command allowlist")
        if any("\x00" in a for a in argv):
            raise ValueError("NUL byte in argument")
        extra = {**self.env, **(env or {})}
        if self.ssh:
            # ssh hands the command to the remote login shell as one string, so every
            # argument is quoted to keep it a single literal word there.
            remote = ["env", *(f"{k}={v}" for k, v in extra.items()), *argv]
            full = [*self.ssh, "--", shlex.join(remote)]
        else:
            full = argv
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), **extra},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input_data), timeout or self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise CommandError(argv, -1, "timed out") from None
        if len(out) > self.max_output:
            out = out[: self.max_output]
        result = Result(
            argv, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
        )
        if check and result.code != 0:
            raise CommandError(argv, result.code, result.stderr)
        return result


def ssh_prefix(host: str, user: str, key_path: str, known_hosts: str, port: int = 22) -> list[str]:
    """SSH with a pinned host key. Unknown or changed keys are refused."""
    return [
        "ssh",
        "-p",
        str(port),
        "-i",
        key_path,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=10",
        f"{user}@{host}",
    ]
