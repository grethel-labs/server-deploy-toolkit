"""All actions OpsTend can run as job steps.

Each action has a description for the preview, the facts its preview depends
on (for stale-preview detection), and is executed by a backend. The live
backend talks to Proxmox, Linux hosts and UniFi; the demo backend simulates
the same effects on the example data so every flow can be tried safely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from opstend.adapters.base import Compute, GuestAction
from opstend.core.jobs import ActionRegistry, Context, FunctionAction
from opstend.core.models import Guest, GuestKind, GuestState, Health
from opstend.core.policy import Category
from opstend.core.store import Store
from opstend.i18n import tr


class Backend(Protocol):
    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def verify(self, action: str, params: dict[str, Any], result: dict[str, Any]) -> bool: ...
    async def undo(self, action: str, params: dict[str, Any], result: dict[str, Any]) -> None: ...


def _name(store: Store, kind: str, oid: str | None) -> str:
    doc = store.get(kind, oid or "") if oid else None
    return str(doc.get("name", oid)) if doc else str(oid)


POWER: dict[str, Callable[[str], str]] = {
    "start": lambda g: tr("Start {guest}", guest=g),
    "stop": lambda g: tr("Force off {guest}", guest=g),
    "shutdown": lambda g: tr("Shut down {guest}", guest=g),
    "reboot": lambda g: tr("Reboot {guest}", guest=g),
    "suspend": lambda g: tr("Pause {guest}", guest=g),
    "resume": lambda g: tr("Resume {guest}", guest=g),
}


def descriptions(store: Store) -> dict[str, tuple[str, Callable[[dict[str, Any]], str]]]:
    n = lambda kind, key: lambda p: _name(store, kind, p.get(key))  # noqa: E731
    g, h, d, s = (
        n("guest", "guest"),
        n("host", "host"),
        n("device", "device"),
        n("service", "service"),
    )
    return {
        "guest.power": (
            Category.POWER,
            lambda p: POWER[p["action"]](g(p)),
        ),
        "guest.snapshot": (
            Category.BACKUP,
            lambda p: tr("Create snapshot '{p_name}' of {g_p}", p_name=p["name"], g_p=g(p)),
        ),
        "guest.snapshot_rollback": (
            Category.POWER,
            lambda p: tr("Roll {g_p} back to snapshot '{p_name}'", g_p=g(p), p_name=p["name"]),
        ),
        "guest.snapshot_delete": (
            Category.BACKUP,
            lambda p: tr("Delete snapshot '{p_name}' of {g_p}", p_name=p["name"], g_p=g(p)),
        ),
        "guest.backup": (
            Category.BACKUP,
            lambda p: tr(
                "Back up {guest} to {destination}",
                guest=g(p),
                destination=p.get("destination") or tr("the default target"),
            ),
        ),
        "guest.clone": (
            Category.DEPLOY,
            lambda p: tr(
                "Create {p_name} from template {g_guest_p_template}",
                p_name=p["name"],
                g_guest_p_template=g({"guest": p["template"]}),
            ),
        ),
        "guest.migrate": (
            Category.RESCHEDULE,
            lambda p: tr("Move {g_p} to {h_p}", g_p=g(p), h_p=h(p)),
        ),
        "service.restart": (
            Category.RESTART,
            lambda p: tr("Restart {s_p} and check its health", s_p=s(p)),
        ),
        "host.patch": (
            Category.PATCH,
            lambda p: (
                tr("Install security updates on {host}", host=h(p))
                if p.get("security_only")
                else tr("Install updates on {host}", host=h(p))
            ),
        ),
        "host.reboot": (Category.POWER, lambda p: tr("Reboot {h_p}", h_p=h(p))),
        "host.bridge_vlan_aware": (
            Category.NETWORK,
            lambda p: tr("Make bridge {bridge} VLAN-aware", bridge=p["bridge"]),
        ),
        "unifi.port_vlans": (
            Category.NETWORK,
            lambda p: tr(
                "Allow VLAN {add_vlan} on {d_p} port {p_port}",
                add_vlan=p["add_vlan"],
                d_p=d(p),
                p_port=p["port"],
            ),
        ),
        "unifi.port_profile": (
            Category.NETWORK,
            lambda p: tr(
                "Assign profile {profile} to {d_p} port {p_port}",
                profile=p["profile"],
                d_p=d(p),
                p_port=p["port"],
            ),
        ),
        "unifi.device_restart": (
            Category.POWER,
            lambda p: tr("Restart {d_p}", d_p=d(p)),
        ),
        "unifi.device_upgrade": (
            Category.UPGRADE,
            lambda p: tr("Install firmware on {d_p}", d_p=d(p)),
        ),
        "unifi.poe_cycle": (
            Category.POWER,
            lambda p: tr("Power-cycle PoE on {d_p} port {p_port}", d_p=d(p), p_port=p["port"]),
        ),
        "unifi.client_block": (
            Category.NETWORK,
            lambda p: (
                tr("Block client {mac}", mac=p["mac"])
                if p.get("block", True)
                else tr("Unblock client {mac}", mac=p["mac"])
            ),
        ),
        "unifi.firewall_block": (
            Category.NETWORK,
            lambda p: tr(
                "Block traffic {name_store_network_p_src} → {name_store_network_p_dst}",
                name_store_network_p_src=_name(store, "network", p["src"]),
                name_store_network_p_dst=_name(store, "network", p["dst"]),
            ),
        ),
        "publication.withdraw": (
            Category.QUARANTINE,
            lambda p: tr(
                "Withdraw public access ({hostname})",
                hostname=(store.get("publication", p["publication"]) or {}).get("hostname"),
            ),
        ),
        "backup.run": (
            Category.BACKUP,
            lambda p: tr("Back up {target}", target=p["target"]),
        ),
        "cluster.replace_node": (
            Category.RESCHEDULE,
            lambda p: tr(
                "Move OpsTend roles off {name_store_node_p_node}",
                name_store_node_p_node=_name(store, "node", p["node"]),
            ),
        ),
        "opstend.self_update": (
            Category.UPGRADE,
            lambda p: tr(
                "Update OpsTend on {name_store_node_p_node} to {version}",
                name_store_node_p_node=_name(store, "node", p["node"]),
                version=p["version"],
            ),
        ),
        "deploy.apply": (
            Category.DEPLOY,
            lambda p: tr("Deploy {spec_name} on {h_p}", spec_name=p["spec"]["name"], h_p=h(p)),
        ),
    }


FACTS: dict[str, Callable[[dict[str, Any], Store], Any]] = {
    "guest.power": lambda p, s: (s.get("guest", p["guest"]) or {}).get("state"),
    "guest.snapshot_rollback": lambda p, s: [
        x["name"] for x in (s.get("guest", p["guest"]) or {}).get("snapshots", [])
    ],
    "unifi.port_vlans": lambda p, s: next(
        (
            x
            for x in (s.get("device", p["device"]) or {}).get("ports", [])
            if x["index"] == p["port"]
        ),
        None,
    ),
    "publication.withdraw": lambda p, s: s.get("publication", p["publication"]),
    "host.patch": lambda p, s: sorted(
        u["id"] for u in s.list("update") if u["target"] == p["host"]
    ),
}


def register(registry: ActionRegistry, store: Store, backend: Backend) -> None:
    for name, (category, describe) in descriptions(store).items():

        def make(
            action: str,
            category: str = category,
            describe: Callable[[dict[str, Any]], str] = describe,
        ) -> FunctionAction:
            async def execute(params: dict[str, Any], ctx: Context) -> dict[str, Any]:
                return await backend.call(action, params)

            async def check(params: dict[str, Any], ctx: Context, result: dict[str, Any]) -> bool:
                return await backend.verify(action, params, result)

            async def undo(params: dict[str, Any], ctx: Context, result: dict[str, Any]) -> None:
                await backend.undo(action, params, result)

            return FunctionAction(
                action,
                category,
                describe=describe,
                execute=execute,
                check=check,
                undo=undo,
                facts=FACTS.get(action),
            )

        registry.register(make(name))


# --- demo backend ------------------------------------------------------------


class DemoBackend:
    """Simulates every action on the example data with realistic delays."""

    def __init__(self, store: Store, delay: float = 0.6) -> None:
        self.store = store
        self.delay = delay

    async def _wait(self, factor: float = 1.0) -> None:
        await asyncio.sleep(self.delay * factor)

    def _guest(self, gid: str) -> dict[str, Any]:
        doc = self.store.get("guest", gid)
        if doc is None:
            raise RuntimeError(f"guest {gid} not found")
        return doc

    async def call(self, action: str, p: dict[str, Any]) -> dict[str, Any]:
        await self._wait()
        s = self.store
        if action == "guest.power":
            g = self._guest(p["guest"])
            before = g["state"]
            g["state"] = {
                "start": "running",
                "resume": "running",
                "reboot": "running",
                "stop": "stopped",
                "shutdown": "stopped",
                "suspend": "paused",
            }[p["action"]]
            s.put("guest", p["guest"], g)
            return {"before": before}
        if action == "guest.snapshot":
            g = self._guest(p["guest"])
            g["snapshots"].append(
                {"name": p["name"], "created_at": None, "description": p.get("description", "")}
            )
            s.put("guest", p["guest"], g)
            return {}
        if action == "guest.snapshot_delete":
            g = self._guest(p["guest"])
            g["snapshots"] = [x for x in g["snapshots"] if x["name"] != p["name"]]
            s.put("guest", p["guest"], g)
            return {}
        if action in ("guest.snapshot_rollback", "guest.backup", "backup.run"):
            await self._wait(2)
            return {"snapshot": f"demo-{action}"}
        if action == "guest.clone":
            tpl = self._guest(p["template"])
            new = dict(
                tpl,
                id=f"{tpl['id']}-{p['name']}",
                name=p["name"],
                template=False,
                state="stopped",
                snapshots=[],
                native_id=str(int(tpl["native_id"]) + 1)
                if tpl["native_id"].isdigit()
                else p["name"],
            )
            s.put("guest", new["id"], new)
            return {"guest": new["id"]}
        if action == "guest.migrate":
            g = self._guest(p["guest"])
            before = g["host_id"]
            g["host_id"] = p["host"]
            s.put("guest", p["guest"], g)
            return {"before": before}
        if action == "service.restart":
            svc = s.get("service", p["service"]) or {}
            await self._wait(1.5)
            svc["health"] = Health.OK
            s.put("service", p["service"], svc)
            return {}
        if action == "host.patch":
            await self._wait(3)
            done = [
                u["id"]
                for u in s.list("update")
                if u["target"] == p["host"] and (u["security"] or not p.get("security_only"))
            ]
            for uid in done:
                s.delete("update", uid)
            for f in s.list("finding"):
                if f["target"] == p["host"] and f["kind"] == "vulnerability":
                    s.delete("finding", f["id"])
            return {"installed": done}
        if action == "unifi.port_vlans":
            dev = s.get("device", p["device"]) or {}
            for port in dev.get("ports", []):
                if port["index"] == p["port"] and p["add_vlan"] not in port["tagged_vlans"]:
                    port["tagged_vlans"] = sorted([*port["tagged_vlans"], p["add_vlan"]])
            s.put("device", p["device"], dev)
            return {"added": p["add_vlan"]}
        if action == "unifi.device_upgrade":
            await self._wait(3)
            dev = s.get("device", p["device"]) or {}
            dev["firmware"], dev["upgrade_to"] = dev.get("upgrade_to") or dev["firmware"], None
            s.put("device", p["device"], dev)
            for u in s.list("update"):
                if u["target"] == p["device"]:
                    s.delete("update", u["id"])
            return {}
        if action == "opstend.self_update":
            await self._wait(2)
            node = s.get("node", p["node"]) or {}
            node["version"] = p["version"]
            s.put("node", p["node"], node)
            for u in s.list("update"):
                if u["target"] == p["node"]:
                    s.delete("update", u["id"])
            return {}
        if action == "publication.withdraw":
            before = s.get("publication", p["publication"])
            s.delete("publication", p["publication"])
            return {"before": before}
        if action == "deploy.apply":
            spec = p["spec"]
            for svc in spec["services"]:
                gid = f"{p['host']}:podman:{spec['name']}-{svc['name']}"
                s.put(
                    "guest",
                    gid,
                    Guest(
                        id=gid,
                        host_id=p["host"],
                        kind=GuestKind.CONTAINER,
                        native_id=f"{spec['name']}-{svc['name']}",
                        name=f"{spec['name']}-{svc['name']}",
                        state=GuestState.RUNNING,
                        image=svc.get("image") or f"localhost/{spec['name']}-{svc['name']}:built",
                    ),
                )
                s.put(
                    "service",
                    f"{spec['name']}-{svc['name']}",
                    {
                        "id": f"{spec['name']}-{svc['name']}",
                        "name": svc["name"].capitalize(),
                        "description": spec.get("source") or "",
                        "guest_id": gid,
                        "host_id": None,
                        "health": "ok",
                        "endpoints": [],
                        "spec": svc,
                        "core": False,
                        "version": None,
                        "exposed": False,
                    },
                )
            return {}
        return {}

    async def verify(self, action: str, p: dict[str, Any], result: dict[str, Any]) -> bool:
        if action == "guest.power":
            want = {
                "start": "running",
                "resume": "running",
                "reboot": "running",
                "stop": "stopped",
                "shutdown": "stopped",
                "suspend": "paused",
            }[p["action"]]
            return bool(self._guest(p["guest"])["state"] == want)
        if action == "unifi.port_vlans":
            dev = self.store.get("device", p["device"]) or {}
            return any(
                x["index"] == p["port"] and p["add_vlan"] in x["tagged_vlans"]
                for x in dev.get("ports", [])
            )
        if action == "publication.withdraw":
            return self.store.get("publication", p["publication"]) is None
        return True

    async def undo(self, action: str, p: dict[str, Any], result: dict[str, Any]) -> None:
        if action == "guest.power" and result.get("before"):
            g = self._guest(p["guest"])
            g["state"] = result["before"]
            self.store.put("guest", p["guest"], g)
        if action == "publication.withdraw" and result.get("before"):
            self.store.put("publication", p["publication"], result["before"])


# --- live backend ------------------------------------------------------------


class LiveBackend:
    """Dispatches actions to the platform adapters of the systems involved."""

    def __init__(
        self,
        store: Store,
        compute: dict[str, Compute],
        unifi: dict[str, Any],
        linux: dict[str, Any],
    ) -> None:
        self.store = store
        self.compute = compute  # host id prefix -> adapter
        self.unifi = unifi  # site -> Unifi adapter
        self.linux = linux  # host id -> Linux adapter (also Proxmox hosts via SSH)
        self.node_update: Any = None  # set by the runtime: (node id, version) -> result

    def _guest(self, gid: str) -> tuple[Guest, Compute]:
        doc = self.store.get("guest", gid)
        if doc is None:
            raise RuntimeError(f"guest {gid} not found")
        guest = Guest.model_validate(doc)
        for prefix, adapter in self.compute.items():
            if guest.host_id.startswith(prefix):
                return guest, adapter
        raise RuntimeError(f"no adapter manages {guest.host_id}")

    async def call(self, action: str, p: dict[str, Any]) -> dict[str, Any]:
        if action == "guest.power":
            guest, adapter = self._guest(p["guest"])
            await adapter.power(guest, GuestAction(p["action"]))
            return {"before": guest.state}
        if action == "guest.snapshot":
            guest, adapter = self._guest(p["guest"])
            await adapter.snapshot(guest, p["name"], p.get("description", ""))
            return {}
        if action == "guest.snapshot_delete":
            guest, adapter = self._guest(p["guest"])
            await adapter.delete_snapshot(guest, p["name"])
            return {}
        if action == "guest.snapshot_rollback":
            guest, adapter = self._guest(p["guest"])
            await adapter.rollback_snapshot(guest, p["name"])
            return {}
        if action == "guest.backup":
            guest, adapter = self._guest(p["guest"])
            return {"backup": await adapter.backup(guest, p["destination"])}
        if action == "guest.clone":
            guest, adapter = self._guest(p["template"])
            return {"native_id": await adapter.clone(guest, p["name"], p.get("host"))}
        if action == "guest.migrate":
            guest, adapter = self._guest(p["guest"])
            await adapter.migrate(guest, p["host"].rsplit(":", 1)[-1])
            return {"before": guest.host_id}
        if action == "opstend.self_update":
            if self.node_update is None:
                raise RuntimeError("cluster transport is not ready")
            result = await self.node_update(p["node"], p["version"])
            if result.get("code") != 0:
                raise RuntimeError(result.get("output", "self-update failed")[-300:])
            return result
        if action == "service.restart":
            svc = self.store.get("service", p["service"]) or {}
            if not svc.get("guest_id"):
                raise RuntimeError(tr("The service has no guest to restart"))
            guest, adapter = self._guest(svc["guest_id"])
            await adapter.power(guest, GuestAction.REBOOT)
            return {}
        if action == "host.patch":
            host = self.linux[p["host"]]
            await host.detect()
            await host.upgrade(security_only=bool(p.get("security_only")))
            return {"reboot": await host.reboot_required()}
        if action.startswith("unifi."):
            dev = self.store.get("device", p.get("device", "")) or {}
            unifi = self.unifi.get(dev.get("site", "default")) or next(iter(self.unifi.values()))
            mac = dev.get("mac", "")
            if action == "unifi.device_restart":
                await unifi.restart_device(mac)
            elif action == "unifi.device_upgrade":
                await unifi.upgrade_device(mac)
            elif action == "unifi.poe_cycle":
                await unifi.power_cycle_port(mac, p["port"])
            elif action == "unifi.client_block":
                await unifi.block_client(p["mac"], p.get("block", True))
            elif action == "unifi.port_profile":
                raw = await unifi.raw_device(mac)
                prev = await unifi.set_port_profile(
                    raw["_id"], p["port"], p["profile"], raw.get("port_overrides", [])
                )
                return {"device_id": raw["_id"], "previous": prev}
            else:
                raise RuntimeError(
                    tr(
                        "{action} needs a port profile; choose one in the network view",
                        action=action,
                    )
                )
            return {}
        raise RuntimeError(tr("{action} is not available on live systems yet", action=action))

    async def verify(self, action: str, p: dict[str, Any], result: dict[str, Any]) -> bool:
        if action == "guest.power":
            guest, adapter = self._guest(p["guest"])
            state = getattr(adapter, "state", None)
            if state is None:
                return True
            want = {
                "start": "running",
                "resume": "running",
                "reboot": "running",
                "stop": "stopped",
                "shutdown": "stopped",
                "suspend": "paused",
            }[p["action"]]
            return str(await state(guest)) == want
        return True

    async def undo(self, action: str, p: dict[str, Any], result: dict[str, Any]) -> None:
        if action == "unifi.port_profile" and "previous" in result:
            dev = self.store.get("device", p["device"]) or {}
            unifi = self.unifi.get(dev.get("site", "default")) or next(iter(self.unifi.values()))
            await unifi._call(
                "PUT", f"/rest/device/{result['device_id']}", {"port_overrides": result["previous"]}
            )
