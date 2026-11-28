"""Turn install requests into workloads, previews and executions."""

from __future__ import annotations

import asyncio
import secrets as pysecrets
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from opstend.adapters.runner import Runner
from opstend.core.models import Host, HostDevice, Model
from opstend.i18n import tr
from opstend.secrets import Vault
from opstend.workloads.catalog import Adapter, SettingType
from opstend.workloads.models import (
    Connection,
    DeviceRequest,
    Environment,
    NetworkPolicy,
    Resources,
    Workload,
    sandbox_from,
)
from opstend.workloads.plan import (
    Command,
    candidates,
    isolation_summary,
    match_devices,
    podman_plan,
    removal_plan,
    reservations,
)


class InstallRequest(Model):
    adapter: str
    id: str
    name: str
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)  # setting name -> value (write-only)
    resources: Resources | None = None
    devices: list[DeviceRequest] | None = None
    network: NetworkPolicy | None = None
    connections: list[Connection] = Field(default_factory=list)
    environment: Environment = Environment.SANDBOX
    sandbox_hours: int = 4
    host: str | None = None


def build(req: InstallRequest, adapter: Adapter, secret_ids: dict[str, str]) -> Workload:
    w = adapter.instantiate(req.id, req.name, req.settings, secret_ids)
    if req.resources:
        w.resources = req.resources
    if req.devices is not None:
        w.devices = req.devices
    if req.network:
        w.network = req.network
    w.connections = req.connections
    if req.environment == Environment.SANDBOX:
        w = sandbox_from(w, req.sandbox_hours)
    w.host_id = req.host
    return w


def secret_placeholders(adapter: Adapter) -> dict[str, str]:
    """Stand-in secret ids so a preview can be computed before any value is stored."""
    return {s.name: f"preview-{s.name}" for s in adapter.settings if s.type == SettingType.SECRET}


def preview(
    w: Workload, adapter: Adapter, hosts: list[Host], workloads: list[Workload]
) -> dict[str, Any]:
    ranked = candidates(
        w, hosts, [], reservations([x for x in workloads if x.id != w.id]), adapter.architectures
    )
    host = next((c for c in ranked if c.host_id == w.host_id), None) or next(
        (c for c in ranked if c.fits), None
    )
    devices: list[HostDevice] = []
    host_doc = next((h for h in hosts if host and h.id == host.host_id), None)
    if host_doc:
        devices, _missing = match_devices(w.devices, host_doc.devices, w.id)
    plan = podman_plan(w, host_doc.address if host_doc else "0.0.0.0", devices)  # noqa: S104
    return {
        "workload": w.model_dump(mode="json"),
        "candidates": [c.__dict__ for c in ranked],
        "host": host.host_id if host else None,
        "isolation": isolation_summary(w),
        "steps": [c.describe for c in plan],
        "problems": []
        if host
        else [tr("No host has enough free resources and the requested devices.")],
    }


def new_secret_id(workload_id: str, name: str) -> str:
    return f"{workload_id}-{name}-{pysecrets.token_hex(4)}"


async def execute(
    plan: list[Command],
    runner: Runner,
    vault: Vault,
    log: Callable[[str], Awaitable[None] | None] | None = None,
) -> list[list[str]]:
    """Run a plan; on failure undo what was done (newest first) and raise."""
    done: list[Command] = []
    for cmd in plan:
        stdin: bytes | None = None
        if cmd.stdin_secret:
            stdin = vault.reveal(cmd.stdin_secret).encode()
        elif cmd.stdin_text is not None:
            stdin = cmd.stdin_text.encode()
        if log:
            result = log(cmd.describe)
            if asyncio.iscoroutine(result):
                await result
        res = await runner.run(cmd.argv, check=False, input_data=stdin, timeout=900)
        if res.code != 0 and cmd.check:
            for prev in reversed(done):
                if prev.undo:
                    await runner.run(prev.undo, check=False)
            raise RuntimeError(f"{cmd.describe}: {res.stderr.strip()[:300] or res.code}")
        done.append(cmd)
    return [c.argv for c in done]


async def remove(w: Workload, runner: Runner, purge: bool) -> None:
    for cmd in removal_plan(w, purge):
        await runner.run(cmd.argv, check=False)


async def smoke_tests(
    w: Workload, runner: Runner, image: str = "docker.io/curlimages/curl:8.16.0"
) -> list[dict[str, Any]]:
    """Run the adapter's checks from inside the workload's own network."""
    results = []
    for test in w.tests:
        if test.get("path") == "tcp":
            argv = [
                "podman",
                "run",
                "--rm",
                "--network",
                w.network_name,
                "--cap-drop=ALL",
                image,
                "-sS",
                "--max-time",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                f"telnet://{test['service']}:{test['port']}",
            ]
        else:
            argv = [
                "podman",
                "run",
                "--rm",
                "--network",
                w.network_name,
                "--cap-drop=ALL",
                image,
                "-sS",
                "--max-time",
                "10",
                "-w",
                "\n%{http_code}",
                f"http://{test['service']}:{test['port']}{test.get('path', '/')}",
            ]
        ok, detail = False, ""
        for _attempt in range(12):
            res = await runner.run(argv, check=False, timeout=60)
            body, _, code = res.stdout.rpartition("\n")
            if test.get("path") == "tcp":
                ok = res.code in (0, 28, 49)  # connected; telnet scheme ends with a timeout
            else:
                ok = (
                    code.strip().isdigit()
                    and int(code) in test.get("expect_status", [200])
                    and (not test.get("expect_text") or test["expect_text"] in body)
                )
            detail = code.strip() or res.stderr.strip()[:120]
            if ok:
                break
            await asyncio.sleep(5)
        results.append({"name": test["name"], "ok": ok, "detail": detail})
    return results
