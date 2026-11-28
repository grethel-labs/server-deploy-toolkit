"""One network graph across UniFi, Proxmox, Linux and VPN.

UniFi knows switches, ports and clients. Proxmox and Linux know bridges, VLAN
tags and guest NICs. VPN and the reverse proxy know tunnels and publications.
OpsTend joins them by MAC address, LLDP and uplink data, so a path can be
followed from a laptop on Wi-Fi through the switch port, the VLAN, the
gateway's firewall, the host's bridge into a container.
"""

from __future__ import annotations

import ipaddress
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from opstend.adapters.base import Bridge
from opstend.core.models import (
    Client,
    Device,
    DeviceKind,
    FirewallRule,
    Guest,
    Host,
    Network,
    Publication,
    RuleAction,
    Tunnel,
)
from opstend.i18n import tr


@dataclass
class Edge:
    a: str
    b: str
    kind: str  # cable | wifi | bridge | vnic | vpn | publish | uplink
    label: str = ""
    a_port: int | None = None
    b_port: int | None = None
    vlans: list[int] = field(default_factory=list)
    native_vlan: int | None = None
    speed_mbps: int | None = None
    inferred: bool = False  # from MAC tables rather than LLDP/uplink data


@dataclass
class Vertex:
    id: str
    kind: str  # gateway | switch | ap | host | bridge | guest | client | internet | service
    name: str
    site: str = "default"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkGraph:
    vertices: dict[str, Vertex] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)
    firewall: list[FirewallRule] = field(default_factory=list)
    mac_owner: dict[str, str] = field(default_factory=dict)

    def add(self, v: Vertex) -> None:
        self.vertices.setdefault(v.id, v)

    def connect(self, edge: Edge) -> None:
        if edge.a in self.vertices and edge.b in self.vertices and edge.a != edge.b:
            for e in self.edges:
                if {e.a, e.b} == {edge.a, edge.b} and e.kind == edge.kind:
                    return
            self.edges.append(edge)

    def neighbors(self, vid: str) -> list[tuple[str, Edge]]:
        out = []
        for e in self.edges:
            if e.a == vid:
                out.append((e.b, e))
            elif e.b == vid:
                out.append((e.a, e))
        return out

    def network_for_vlan(self, vlan: int | None) -> Network | None:
        for n in self.networks:
            if (n.vlan or 1) == (vlan or 1):
                return n
        return None

    def network_for_ip(self, ip: str | None) -> Network | None:
        if not ip:
            return None
        try:
            addr = ipaddress.ip_address(ip.split("/")[0])
        except ValueError:
            return None
        for n in self.networks:
            if n.subnet and addr in ipaddress.ip_network(n.subnet, strict=False):
                return n
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "vertices": [v.__dict__ for v in self.vertices.values()],
            "edges": [e.__dict__ for e in self.edges],
        }


def _mac(m: str | None) -> str | None:
    return m.lower() if m else None


def build(
    *,
    devices: list[Device],
    clients: list[Client],
    networks: list[Network],
    firewall: list[FirewallRule],
    hosts: list[Host],
    guests: list[Guest],
    bridges: list[Bridge],
    tunnels: list[Tunnel] | None = None,
    publications: list[Publication] | None = None,
    host_macs: dict[str, list[str]] | None = None,
) -> NetworkGraph:
    g = NetworkGraph(networks=list(networks), firewall=list(firewall))
    kind = {
        DeviceKind.GATEWAY: "gateway",
        DeviceKind.SWITCH: "switch",
        DeviceKind.ACCESS_POINT: "ap",
        DeviceKind.OTHER: "device",
    }
    by_mac: dict[str, Device] = {}
    for d in devices:
        g.add(
            Vertex(
                d.id,
                kind[DeviceKind(d.kind)],
                d.name,
                d.site,
                {
                    "model": d.model,
                    "ip": d.address,
                    "health": d.health,
                    "firmware": d.firmware,
                    "upgrade_to": d.upgrade_to,
                },
            )
        )
        by_mac[d.mac] = d
        g.mac_owner[d.mac] = d.id
    if any(DeviceKind(d.kind) == DeviceKind.GATEWAY for d in devices):
        g.add(Vertex("internet", "internet", "Internet"))
    for h in hosts:
        g.add(
            Vertex(
                h.id,
                "host",
                h.name,
                h.site,
                {"platform": h.platform, "health": h.health, "address": h.address},
            )
        )
    for mac_list_host, macs in (host_macs or {}).items():
        for m in macs:
            g.mac_owner[m.lower()] = mac_list_host
    for b in bridges:
        bid = f"{b.host_id}/{b.name}"
        g.add(
            Vertex(
                bid,
                "bridge",
                b.name,
                data={"vlan_aware": b.vlan_aware, "address": b.address, "host": b.host_id},
            )
        )
        g.connect(Edge(b.host_id, bid, "bridge"))
        if b.mac:
            g.mac_owner[b.mac.lower()] = b.host_id
    for gu in guests:
        g.add(
            Vertex(
                gu.id,
                "guest",
                gu.name,
                data={"kind": gu.kind, "state": gu.state, "host": gu.host_id},
            )
        )
        for nic in gu.nics:
            if nic.mac:
                g.mac_owner[nic.mac.lower()] = gu.id
            target = f"{gu.host_id}/{nic.bridge}" if nic.bridge else gu.host_id
            if target not in g.vertices:
                target = gu.host_id
            g.connect(
                Edge(
                    gu.id,
                    target,
                    "vnic",
                    label=nic.name,
                    vlans=[nic.vlan] if nic.vlan else [],
                    native_vlan=nic.vlan,
                )
            )

    # Device uplinks: the most reliable source.
    for d in devices:
        if d.uplink_mac and d.uplink_mac in by_mac:
            parent = by_mac[d.uplink_mac]
            local = next((p for p in d.ports if p.is_uplink), None)
            remote = next((p for p in parent.ports if p.index == d.uplink_port), None)
            g.connect(
                Edge(
                    parent.id,
                    d.id,
                    "cable",
                    a_port=d.uplink_port,
                    b_port=local.index if local else None,
                    speed_mbps=(remote.speed_mbps if remote else None)
                    or (local.speed_mbps if local else None),
                    vlans=remote.tagged_vlans if remote else [],
                    native_vlan=remote.native_vlan if remote else None,
                    label=f"P{d.uplink_port}" if d.uplink_port else "",
                )
            )
        if DeviceKind(d.kind) == DeviceKind.GATEWAY:
            g.connect(Edge("internet", d.id, "uplink", label="WAN"))

    # LLDP neighbors identify hosts (and other devices) on switch ports.
    claimed: set[tuple[str, int]] = set()
    for d in devices:
        for p in d.ports:
            if not p.lldp:
                continue
            owner = g.mac_owner.get(p.lldp.chassis)
            if owner is None:
                owner = next((h.id for h in hosts if p.lldp.name and h.name == p.lldp.name), None)
            if owner and owner != d.id:
                g.connect(
                    Edge(
                        d.id,
                        owner,
                        "cable",
                        a_port=p.index,
                        label=f"P{p.index}",
                        speed_mbps=p.speed_mbps,
                        vlans=p.tagged_vlans,
                        native_vlan=p.native_vlan,
                    )
                )
                claimed.add((d.id, p.index))

    # Clients: wired ones sit on a switch port, wireless ones on an AP.
    for c in clients:
        owner = g.mac_owner.get(c.mac)
        if c.device_mac not in by_mac:
            continue
        dev = by_mac[c.device_mac]
        if owner and g.vertices[owner].kind in ("guest", "host"):
            # A guest seen on a switch port reveals which port its host uses.
            host_id = g.vertices[owner].data.get("host", owner)
            if (
                c.wired
                and c.port is not None
                and (dev.id, c.port) not in claimed
                and host_id in g.vertices
            ):
                port = next((p for p in dev.ports if p.index == c.port), None)
                g.connect(
                    Edge(
                        dev.id,
                        host_id,
                        "cable",
                        a_port=c.port,
                        label=f"P{c.port}",
                        speed_mbps=port.speed_mbps if port else None,
                        vlans=port.tagged_vlans if port else [],
                        native_vlan=port.native_vlan if port else None,
                        inferred=True,
                    )
                )
                claimed.add((dev.id, c.port))
            continue
        cid = f"client:{c.mac}"
        g.add(
            Vertex(
                cid,
                "client",
                c.name or c.mac,
                data={"ip": c.ip, "vlan": c.vlan, "signal": c.signal_dbm},
            )
        )
        g.connect(
            Edge(
                dev.id,
                cid,
                "cable" if c.wired else "wifi",
                a_port=c.port,
                label=f"P{c.port}"
                if c.wired and c.port
                else (f"{c.signal_dbm} dBm" if c.signal_dbm else "WLAN"),
                native_vlan=c.vlan,
            )
        )

    for t in tunnels or []:
        g.connect(Edge(t.a_host, t.b_host, "vpn", label=t.name))
    for pub in publications or []:
        sid = f"service:{pub.service_id}"
        g.add(Vertex(sid, "service", pub.hostname, data={"public": pub.public}))
        g.connect(Edge(pub.proxy_host, sid, "publish", label=pub.hostname))
    return g


# --- reachability ----------------------------------------------------------


def l2_path(g: NetworkGraph, src: str, dst: str, vlan: int | None) -> list[str] | None:
    """Layer-2 path that carries `vlan` end to end, or None."""
    want = vlan or 1
    queue: deque[list[str]] = deque([[src]])
    seen = {src}
    while queue:
        path = queue.popleft()
        here = path[-1]
        if here == dst:
            return path
        for nxt, e in g.neighbors(here):
            if nxt in seen or e.kind in ("vpn", "publish", "uplink"):
                continue
            carries = (
                (not e.vlans and e.native_vlan is None)
                or want in e.vlans
                or (e.native_vlan or 1) == want
            )
            if e.kind in ("bridge", "wifi"):
                carries = True
            if not carries:
                continue
            seen.add(nxt)
            queue.append([*path, nxt])
    return None


def firewall_decision(
    g: NetworkGraph, src: Network | None, dst: Network | None, port: int | None = None
) -> tuple[bool, str]:
    if src is None or dst is None:
        return True, tr("unknown network; no rule applied")
    if src.id == dst.id:
        return True, tr("same network")
    for rule in sorted(g.firewall, key=lambda r: r.order):
        if not rule.enabled:
            continue
        if rule.src not in ("any", f"net:{src.name}") or rule.dst not in ("any", f"net:{dst.name}"):
            continue
        if rule.ports and port is not None and str(port) not in rule.ports.split(","):
            continue
        allowed = RuleAction(rule.action) == RuleAction.ALLOW
        return allowed, tr("rule '{name}' ({action})", name=rule.name, action=rule.action)
    if src.isolated or dst.isolated:
        return False, (
            tr("the source network is isolated")
            if src.isolated
            else tr("the target network is isolated")
        )
    return True, tr("default: routed between internal networks")


@dataclass
class PathExplanation:
    reachable: bool
    hops: list[str]
    steps: list[str]


def explain(g: NetworkGraph, src: str, dst: str, port: int | None = None) -> PathExplanation:
    """Explain in words how (or why not) `src` reaches `dst`."""

    def vlan_and_net(vid: str) -> tuple[int | None, Network | None]:
        v = g.vertices[vid]
        if v.kind == "client":
            net = g.network_for_vlan(v.data.get("vlan")) or g.network_for_ip(v.data.get("ip"))
            return (net.vlan if net else v.data.get("vlan")), net
        if v.kind == "guest":
            for _, e in g.neighbors(vid):
                if e.kind == "vnic":
                    net = g.network_for_vlan(e.native_vlan)
                    return e.native_vlan, net
        if v.kind == "host":
            net = g.network_for_ip(v.data.get("address"))
            return (net.vlan if net else None), net
        return None, None

    if src not in g.vertices or dst not in g.vertices:
        return PathExplanation(False, [], [tr("unknown source or target")])
    svlan, snet = vlan_and_net(src)
    dvlan, dnet = vlan_and_net(dst)
    steps: list[str] = []
    if (svlan or 1) == (dvlan or 1):
        path = l2_path(g, src, dst, svlan)
        if path:
            steps.append(tr("same VLAN {vlan}: switched directly", vlan=svlan or 1))
            return PathExplanation(True, path, steps)
        return PathExplanation(
            False, [], [tr("no link carries VLAN {vlan} between them", vlan=svlan or 1)]
        )
    gw = next((v.id for v in g.vertices.values() if v.kind == "gateway"), None)
    if gw is None:
        return PathExplanation(False, [], [tr("different VLANs and no gateway known")])
    first = l2_path(g, src, gw, svlan)
    if not first:
        return PathExplanation(
            False, [], [tr("source cannot reach the gateway in VLAN {vlan}", vlan=svlan or 1)]
        )
    steps.append(
        tr(
            "VLAN {vlan} ({network}) to the gateway",
            vlan=svlan or 1,
            network=snet.name if snet else "?",
        )
    )
    allowed, why = firewall_decision(g, snet, dnet, port)
    steps.append(tr("gateway: {why}", why=why))
    if not allowed:
        return PathExplanation(False, first, steps)
    second = l2_path(g, gw, dst, dvlan)
    if not second:
        steps.append(tr("no link carries VLAN {vlan} to the target", vlan=dvlan or 1))
        return PathExplanation(False, first, steps)
    steps.append(
        tr(
            "VLAN {vlan} ({network}) to the target",
            vlan=dvlan or 1,
            network=dnet.name if dnet else "?",
        )
    )
    return PathExplanation(True, first + second[1:], steps)
