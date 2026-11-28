"""Privileged host agent.

The web app, API and automation run as the unprivileged `opstend` user. Only
this small agent runs as root. It listens on a Unix socket that only the
`opstend` user may use (checked with SO_PEERCRED), accepts argument lists,
checks them against the command allowlist and a short deny list, runs them
without a shell and returns the result. Every request is logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pwd
import re
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opstend.adapters.runner import ALLOWED, CommandError, Result, Runner

log = logging.getLogger("opstend.agent")

SOCKET = Path("/run/opstend/agent.sock")
MAX_REQUEST = 1024 * 1024

# Arguments that would turn an allowed program into a general shell or editor.
DENIED_ARGS = {
    ("podman", "exec"),
    ("podman", "run", "--privileged"),
    ("systemctl", "edit"),
    ("journalctl", "--vacuum-time"),
    ("apt-get", "remove"),
    ("apt-get", "purge"),
    ("dnf", "remove"),
    ("apk", "del"),
    ("pacman", "-R"),
    ("zypper", "remove"),
}
READABLE = ("/etc/os-release", "/proc/", "/var/run/reboot-required", "/sys/class/net/")


def check(argv: list[str]) -> None:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise PermissionError("argv must be a list of strings")
    prog = os.path.basename(argv[0])
    if prog == "opstend" and argv[1:2] == ["self-update"] and len(argv) <= 4:
        if len(argv) == 4 and (
            argv[2] != "--version" or not re.fullmatch(r"[0-9][0-9a-z.-]{0,31}", argv[3])
        ):
            raise PermissionError("self-update accepts only --version <version>")
        return
    if prog not in ALLOWED or prog in ("env", "opstend"):
        raise PermissionError(f"{prog} is not allowed through the agent")
    for denied in DENIED_ARGS:
        if prog == denied[0] and all(d in argv[1:] for d in denied[1:]):
            raise PermissionError(f"{' '.join(denied)} is not allowed through the agent")
    if prog == "nft" and argv[1:2] != ["list"]:
        raise PermissionError("nft may only list rules through the agent; changes are jobs")
    if prog == "udevadm" and argv[1:] != ["info", "--export-db"]:
        raise PermissionError("udevadm may only list devices")
    if prog == "sshd" and argv[1:] != ["-T"]:
        raise PermissionError("sshd may only print its configuration")
    if prog == "cat" and not all(a.startswith(READABLE) for a in argv[1:]):
        raise PermissionError("cat may only read system facts")


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


@dataclass
class Agent:
    user: str = "opstend"
    path: Path = SOCKET
    runner: Runner | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            uid = peer_uid(sock)
            allowed = {0, pwd.getpwnam(self.user).pw_uid}
            if uid not in allowed:
                raise PermissionError(f"uid {uid} may not use the agent")
            line = await reader.readline()
            if len(line) > MAX_REQUEST:
                raise ValueError("request too large")
            req = json.loads(line)
            argv = req["argv"]
            check(argv)
            env = {
                k: v
                for k, v in (req.get("env") or {}).items()
                if k in ("DEBIAN_FRONTEND", "LC_ALL", "LANG")
            }
            runner = self.runner or Runner()
            stdin = base64.b64decode(req["stdin"]) if req.get("stdin") else None
            result = await runner.run(
                argv,
                check=False,
                timeout=float(req.get("timeout") or 120),
                env=env,
                input_data=stdin,
            )
            log.info("agent ran %s -> %s", argv[:3], result.code)
            out: dict[str, Any] = {
                "code": result.code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except (PermissionError, ValueError, KeyError, CommandError) as exc:
            log.warning("agent refused request: %s", exc)
            out = {"error": str(exc)}
        writer.write(json.dumps(out).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def serve(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        server = await asyncio.start_unix_server(self.handle, path=str(self.path))
        group = pwd.getpwnam(self.user).pw_gid
        os.chown(self.path, 0, group)
        os.chmod(self.path, 0o660)
        log.info("agent listening on %s", self.path)
        async with server:
            await server.serve_forever()


class AgentRunner(Runner):
    """Runner used by the unprivileged service: forwards commands to the agent."""

    def __init__(self, path: Path = SOCKET, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.path = path

    async def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        input_data: bytes | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Result:
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        writer.write(
            json.dumps(
                {
                    "argv": argv,
                    "env": env or {},
                    "timeout": timeout or self.timeout,
                    "stdin": base64.b64encode(input_data).decode() if input_data else None,
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        data = json.loads(await reader.readline())
        writer.close()
        if "error" in data:
            raise PermissionError(data["error"])
        result = Result(argv, int(data["code"]), data["stdout"], data["stderr"])
        if check and result.code != 0:
            raise CommandError(argv, result.code, result.stderr)
        return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if os.geteuid() != 0:
        log.error("the agent must run as root")
        return 1
    asyncio.run(Agent().serve())
    return 0
