"""Network insights across system boundaries.

Each check looks at facts that no single system sees on its own, for example
a Proxmox guest tagged with a VLAN that its host's switch port does not carry.
Every insight names the affected objects, explains the impact in plain words
and, where possible, proposes a job that fixes it.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from opstend.core.models import Device, Finding, FindingKind, Severity
from opstend.i18n import tr
from opstend.network.graph import NetworkGraph, firewall_decision


@dataclass
class Insight:
    code: str
    severity: Severity
    title: str
    detail: str
    objects: list[str]
    fix: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)

    def finding(self) -> Finding:
        key = hashlib.sha1(
            f"{self.code}:{'|'.join(sorted(self.objects))}".encode(), usedforsecurity=False
        ).hexdigest()[:12]
        return Finding(
            id=f"net-{key}",
            target=self.objects[0] if self.objects else "network",
            kind=FindingKind.NETWORK,
            severity=self.severity,
            title=self.title,
            detail=self.detail,
            recommendation=(self.fix or {}).get("summary"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "objects": self.objects,
            "fix": self.fix,
            "tags": self.tags,
            "id": self.finding().id,
        }


def vlan_mismatch(g: NetworkGraph) -> list[Insight]:
    """Guest VLAN tags that the host's switch port does not carry."""
    out = []
    for gid, gv in g.vertices.items():
        if gv.kind != "guest":
            continue
        host: str | None = gv.data.get("host")
        uplinks = [e for _, e in g.neighbors(host) if e.kind == "cable"] if host else []
        if not uplinks or host is None:
            continue
        for _, vnic in g.neighbors(gid):
            if vnic.kind != "vnic" or not vnic.native_vlan:
                continue
            vlan = vnic.native_vlan
            carried = [
                e
                for e in uplinks
                if vlan in e.vlans
                or (e.native_vlan or 1) == vlan
                or (not e.vlans and e.native_vlan is None)
            ]
            if carried:
                continue
            port = uplinks[0]
            switch = port.a if port.b == host else port.b
            sw = g.vertices[switch].name
            hname = g.vertices[host].name
            net = g.network_for_vlan(vlan)
            netname = f" ({net.name})" if net else ""
            tagged = ", ".join(str(v) for v in port.vlans) or "–"
            out.append(
                Insight(
                    code="vlan-not-carried",
                    severity=Severity.HIGH,
                    title=tr(
                        "{gv_name} uses VLAN {vlan}, but {sw} port {port_a_port} does not carry it",
                        gv_name=gv.name,
                        vlan=vlan,
                        sw=sw,
                        port_a_port=port.a_port,
                    ),
                    detail=tr(
                        "The guest's NIC is tagged {vlan}{netname}. The switch port to {hname} carries native {port_native_vlan_or_1} and tagged {tagged}. The switch drops this guest's traffic.",
                        vlan=vlan,
                        netname=netname,
                        hname=hname,
                        port_native_vlan_or_1=port.native_vlan or 1,
                        tagged=tagged,
                    ),
                    objects=[gid, host, switch],
                    fix={
                        "summary": tr(
                            "Allow VLAN {vlan} on {sw} port {port_a_port}",
                            vlan=vlan,
                            sw=sw,
                            port_a_port=port.a_port,
                        ),
                        "action": "unifi.port_vlans",
                        "device": switch,
                        "port": port.a_port,
                        "add_vlan": vlan,
                    },
                    tags=["proxmox", "unifi"],
                )
            )
    return out


def bridge_not_vlan_aware(g: NetworkGraph) -> list[Insight]:
    out = []
    for bid, bv in g.vertices.items():
        if bv.kind != "bridge" or bv.data.get("vlan_aware"):
            continue
        tagged = [n for n, e in g.neighbors(bid) if e.kind == "vnic" and e.native_vlan]
        if tagged:
            names = ", ".join(g.vertices[x].name for x in tagged)
            out.append(
                Insight(
                    code="bridge-not-vlan-aware",
                    severity=Severity.HIGH,
                    title=tr(
                        "Bridge {bv_name} is not VLAN-aware but carries tagged guests",
                        bv_name=bv.name,
                    ),
                    detail=tr(
                        "{names} use VLAN tags on {bv_name}. Without VLAN awareness the tags do not reach the physical network as intended.",
                        names=names,
                        bv_name=bv.name,
                    ),
                    objects=[bid, *tagged],
                    fix={
                        "summary": tr(
                            "Make {bv_name} VLAN-aware (short network interruption)",
                            bv_name=bv.name,
                        ),
                        "action": "host.bridge_vlan_aware",
                        "bridge": bid,
                    },
                )
            )
    return out


def _peer_is_infra(g: NetworkGraph, dev_id: str, port: int) -> bool:
    for nxt, e in g.neighbors(dev_id):
        if (
            e.a == dev_id
            and e.a_port == port
            and g.vertices[nxt].kind in ("switch", "ap", "gateway", "host")
        ):
            return True
    return False


def slow_links(g: NetworkGraph, devices: list[Device]) -> list[Insight]:
    """Gigabit-capable links to infrastructure that negotiated 100 Mbit/s or less."""
    out = []
    for d in devices:
        for p in d.ports:
            if not (p.up and p.speed_mbps and p.max_speed_mbps):
                continue
            if p.speed_mbps <= 100 < p.max_speed_mbps and (
                p.is_uplink or p.lldp is not None or _peer_is_infra(g, d.id, p.index)
            ):
                peer = f" ({p.lldp.name})" if p.lldp and p.lldp.name else ""
                out.append(
                    Insight(
                        code="link-downgraded",
                        severity=Severity.MEDIUM,
                        title=tr(
                            "{d_name} port {index}{peer} runs at {speed_mbps} instead of {max_speed_mbps} Mbit/s",
                            d_name=d.name,
                            index=p.index,
                            peer=peer,
                            speed_mbps=p.speed_mbps,
                            max_speed_mbps=p.max_speed_mbps,
                        ),
                        detail=tr(
                            "Both sides support a faster link. A damaged cable or a bad connector usually causes this. Replace the cable or reseat the connector."
                        ),
                        objects=[d.id],
                        tags=["cabling"],
                    )
                )
    return out


def port_errors(devices: list[Device], threshold: int = 100) -> list[Insight]:
    out = []
    for d in devices:
        for p in d.ports:
            errors = p.rx_errors + p.tx_errors
            if errors >= threshold:
                out.append(
                    Insight(
                        code="port-errors",
                        severity=Severity.MEDIUM,
                        title=tr(
                            "{d_name} port {index} has {errors} transmission errors",
                            d_name=d.name,
                            index=p.index,
                            errors=errors,
                        ),
                        detail=tr(
                            "Frequent errors point to a bad cable, interference or a duplex mismatch."
                        ),
                        objects=[d.id],
                    )
                )
            if p.up and not p.full_duplex:
                out.append(
                    Insight(
                        code="half-duplex",
                        severity=Severity.MEDIUM,
                        title=tr(
                            "{d_name} port {index} runs half duplex", d_name=d.name, index=p.index
                        ),
                        detail=tr(
                            "Half duplex causes collisions and slow transfers. Check fixed speed settings on the other side."
                        ),
                        objects=[d.id],
                    )
                )
    return out


def poe_budget(devices: list[Device], warn: float = 0.8) -> list[Insight]:
    out = []
    for d in devices:
        if not d.poe_budget_watts:
            continue
        used = sum(p.poe_watts or 0 for p in d.ports)
        if used >= warn * d.poe_budget_watts:
            out.append(
                Insight(
                    code="poe-budget",
                    severity=Severity.HIGH if used >= d.poe_budget_watts else Severity.MEDIUM,
                    title=tr(
                        "{d_name} uses {used:.0f} of {d_poe_budget_watts:.0f} W PoE budget",
                        d_name=d.name,
                        used=used,
                        d_poe_budget_watts=d.poe_budget_watts,
                    ),
                    detail=tr(
                        "When the budget runs out the switch turns PoE ports off. Move a powered device to another switch or use an injector."
                    ),
                    objects=[d.id],
                )
            )
    return out


def single_points_of_failure(g: NetworkGraph, voters: list[str]) -> list[Insight]:
    """All control-plane voters behind one switch: one failure stops self-management."""
    if len(voters) < 2:
        return []
    behind: dict[str, set[str]] = defaultdict(set)
    for host in voters:
        if host not in g.vertices:
            return []
        for nxt, e in g.neighbors(host):
            if e.kind == "cable" and g.vertices[nxt].kind == "switch":
                behind[nxt].add(host)
    for switch, hosts in behind.items():
        if hosts == set(voters):
            sw = g.vertices[switch].name
            return [
                Insight(
                    code="voters-share-switch",
                    severity=Severity.MEDIUM,
                    title=tr("All OpsTend voters depend on {sw}", sw=sw),
                    detail=tr(
                        "If this switch fails, the control plane loses its majority and automatic repairs pause until it returns. Connect one voter to another switch."
                    ),
                    objects=[switch, *sorted(hosts)],
                )
            ]
    return []


def duplicate_ips(g: NetworkGraph) -> list[Insight]:
    seen: dict[str, list[str]] = defaultdict(list)
    for vid, v in g.vertices.items():
        ip = v.data.get("ip") or v.data.get("address")
        if ip and "/" not in str(ip) and not str(ip).startswith("http"):
            seen[str(ip)].append(vid)
    return [
        Insight(
            code="duplicate-ip",
            severity=Severity.HIGH,
            title=tr("{ip} is used by {len_ids} systems", ip=ip, len_ids=len(ids)),
            detail=tr("Duplicate addresses cause intermittent outages. Use DHCP reservations."),
            objects=ids,
        )
        for ip, ids in seen.items()
        if len(ids) > 1
    ]


def wrong_subnet(g: NetworkGraph) -> list[Insight]:
    out = []
    for vid, v in g.vertices.items():
        if v.kind != "client" or not v.data.get("ip"):
            continue
        by_ip = g.network_for_ip(v.data["ip"])
        by_vlan = g.network_for_vlan(v.data.get("vlan"))
        if by_ip and by_vlan and by_ip.id != by_vlan.id:
            out.append(
                Insight(
                    code="client-wrong-subnet",
                    severity=Severity.MEDIUM,
                    title=tr(
                        "{v_name} has an address of {by_ip_name} but sits in {by_vlan_name}",
                        v_name=v.name,
                        by_ip_name=by_ip.name,
                        by_vlan_name=by_vlan.name,
                    ),
                    detail=tr(
                        "A static address that does not match the VLAN breaks routing for this client."
                    ),
                    objects=[vid],
                )
            )
    return out


def weak_wifi(g: NetworkGraph, threshold: int = -75) -> list[Insight]:
    out = []
    for vid, v in g.vertices.items():
        signal = v.data.get("signal")
        if v.kind == "client" and isinstance(signal, int) and signal <= threshold:
            out.append(
                Insight(
                    code="weak-wifi",
                    severity=Severity.LOW,
                    title=tr(
                        "{v_name} has a weak Wi-Fi signal ({signal} dBm)",
                        v_name=v.name,
                        signal=signal,
                    ),
                    detail=tr("Below -75 dBm throughput drops and roaming becomes unreliable."),
                    objects=[vid],
                )
            )
    return out


def segmentation(g: NetworkGraph) -> list[Insight]:
    """Less trusted networks that may reach server networks."""
    out = []
    risky = [n for n in g.networks if n.purpose == "guest" or "iot" in n.name.lower()]
    servers = [n for n in g.networks if "server" in n.name.lower() or n.purpose == "server"]
    for src in risky:
        for dst in servers:
            allowed, why = firewall_decision(g, src, dst)
            if allowed:
                out.append(
                    Insight(
                        code="segmentation-open",
                        severity=Severity.HIGH,
                        title=tr(
                            "{src_name} can reach {dst_name}", src_name=src.name, dst_name=dst.name
                        ),
                        detail=tr(
                            "Devices in {src_name} are usually less trusted. Reason: {why}.",
                            src_name=src.name,
                            why=why,
                        ),
                        objects=[f"network:{src.id}", f"network:{dst.id}"],
                        fix={
                            "summary": tr(
                                "Block {src_name} → {dst_name} except required ports",
                                src_name=src.name,
                                dst_name=dst.name,
                            ),
                            "action": "unifi.firewall_block",
                            "src": src.id,
                            "dst": dst.id,
                        },
                    )
                )
    return out


def analyze(
    g: NetworkGraph, devices: list[Device], voters: list[str] | None = None
) -> list[Insight]:
    checks: list[list[Insight]] = [
        vlan_mismatch(g),
        bridge_not_vlan_aware(g),
        slow_links(g, devices),
        port_errors(devices),
        poe_budget(devices),
        single_points_of_failure(g, voters or []),
        duplicate_ips(g),
        wrong_subnet(g),
        weak_wifi(g),
        segmentation(g),
    ]
    order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
    return sorted((i for c in checks for i in c), key=lambda i: order[Severity(i.severity)])
