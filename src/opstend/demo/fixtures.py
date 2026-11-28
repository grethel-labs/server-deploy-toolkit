"""A realistic example site for the demo mode, tests and screenshots.

Workshop site ("Werkstatt"): a UniFi gateway, two switches and two access
points, one Proxmox host, three Linux servers on different architectures and
distributions, plus an external Ubuntu host that publishes a service through
WireGuard. The data contains a few deliberate problems so every view has
something to show.
"""

from __future__ import annotations

import zlib
from datetime import UTC, datetime, timedelta

from opstend.adapters.base import Bridge
from opstend.core.models import (
    Client,
    Device,
    DeviceKind,
    Finding,
    FindingKind,
    FirewallRule,
    Guest,
    GuestKind,
    GuestState,
    Health,
    Host,
    HostDevice,
    Lldp,
    Network,
    Nic,
    Node,
    NodeState,
    Observed,
    Platform,
    Port,
    Publication,
    Resources,
    RuleAction,
    Service,
    Severity,
    Snapshot,
    Tunnel,
    Update,
    Usage,
)
from opstend.core.store import Store

NOW = datetime.now(UTC)


def _obs(source: str, age_s: int = 30) -> Observed:
    return Observed(source=source, observed_at=NOW - timedelta(seconds=age_s))


NETWORKS = [
    Network(
        id="net-lan",
        name="LAN",
        subnet="10.20.10.0/24",
        gateway="10.20.10.1",
        dhcp_start="10.20.10.100",
        dhcp_stop="10.20.10.199",
    ),
    Network(id="net-srv", name="Server", vlan=20, subnet="10.20.20.0/24", gateway="10.20.20.1"),
    Network(id="net-iot", name="IoT", vlan=30, subnet="10.20.30.0/24", gateway="10.20.30.1"),
    Network(
        id="net-guest",
        name="Gäste",
        vlan=40,
        subnet="10.20.40.0/24",
        gateway="10.20.40.1",
        purpose="guest",
        isolated=True,
    ),
]

FIREWALL = [
    FirewallRule(
        id="fw-1",
        name="Gäste nach intern blockieren",
        action=RuleAction.DROP,
        src="net:Gäste",
        dst="any",
        order=2000,
    ),
    FirewallRule(
        id="fw-2",
        name="IoT darf MQTT zum Server",
        action=RuleAction.ALLOW,
        src="net:IoT",
        dst="net:Server",
        protocol="tcp",
        ports="1883",
        order=2100,
    ),
]


def _sw_ports() -> list[Port]:
    return [
        Port(index=1, name="Uplink", up=True, speed_mbps=1000, max_speed_mbps=1000, is_uplink=True),
        Port(
            index=2,
            name="nuc-a",
            up=True,
            speed_mbps=1000,
            max_speed_mbps=1000,
            native_vlan=1,
            tagged_vlans=[20, 30],
            profile="Server-Trunk",
            lldp=Lldp(chassis="bc:24:11:0a:00:01", port="enp1s0", name="nuc-a"),
        ),
        Port(
            index=3,
            name="host-b",
            up=True,
            speed_mbps=1000,
            max_speed_mbps=1000,
            native_vlan=1,
            tagged_vlans=[20],
            profile="Server-Trunk",
            lldp=Lldp(chassis="bc:24:11:0b:00:01", port="eth0", name="host-b"),
        ),
        Port(
            index=4,
            name="nuc-c",
            up=True,
            speed_mbps=100,
            max_speed_mbps=1000,
            native_vlan=1,
            tagged_vlans=[20],
            profile="Server-Trunk",
            rx_errors=1843,
            lldp=Lldp(chassis="bc:24:11:0c:00:01", port="eno1", name="nuc-c"),
        ),
        Port(
            index=5,
            name="ap-werkstatt",
            up=True,
            speed_mbps=1000,
            max_speed_mbps=1000,
            poe_enabled=True,
            poe_watts=13.4,
        ),
        Port(
            index=6,
            name="pi-sensor",
            up=True,
            speed_mbps=1000,
            max_speed_mbps=1000,
            poe_enabled=True,
            poe_watts=6.8,
            native_vlan=30,
            profile="IoT",
            lldp=Lldp(chassis="dc:a6:32:0e:00:01", port="eth0", name="pi-sensor"),
        ),
        Port(
            index=7,
            name="Kamera Hof",
            up=True,
            speed_mbps=100,
            max_speed_mbps=100,
            poe_enabled=True,
            poe_watts=21.5,
            native_vlan=30,
            profile="IoT",
        ),
        Port(index=8, name="frei", up=False, max_speed_mbps=1000),
    ]


DEVICES = [
    Device(
        id="unifi:gw",
        name="gw-werkstatt",
        kind=DeviceKind.GATEWAY,
        mac="78:45:58:00:00:01",
        model="UDM Pro",
        address="10.20.10.1",
        firmware="4.1.13",
        health=Health.OK,
        site="Werkstatt",
        observed=_obs("unifi-api"),
        ports=[
            Port(index=8, name="LAN", up=True, speed_mbps=1000, max_speed_mbps=10000),
            Port(index=9, name="WAN", up=True, speed_mbps=1000, max_speed_mbps=1000),
        ],
    ),
    Device(
        id="unifi:sw",
        name="sw-werkstatt",
        kind=DeviceKind.SWITCH,
        mac="78:45:58:00:00:02",
        model="USW-Lite-8-PoE",
        address="10.20.10.2",
        firmware="7.1.26",
        upgrade_to="7.2.123",
        health=Health.WARNING,
        site="Werkstatt",
        uplink_mac="78:45:58:00:00:01",
        uplink_port=8,
        poe_budget_watts=52,
        ports=_sw_ports(),
        observed=_obs("unifi-api"),
    ),
    Device(
        id="unifi:ap1",
        name="ap-werkstatt",
        kind=DeviceKind.ACCESS_POINT,
        mac="78:45:58:00:00:03",
        model="U7 Pro",
        address="10.20.10.3",
        firmware="8.0.19",
        health=Health.OK,
        site="Werkstatt",
        uplink_mac="78:45:58:00:00:02",
        uplink_port=5,
        ports=[
            Port(
                index=1,
                name="Uplink",
                up=True,
                speed_mbps=1000,
                max_speed_mbps=2500,
                is_uplink=True,
            )
        ],
        observed=_obs("unifi-api"),
    ),
    Device(
        id="unifi:ap2",
        name="ap-buero",
        kind=DeviceKind.ACCESS_POINT,
        mac="78:45:58:00:00:04",
        model="U6 Lite",
        address="10.20.10.4",
        firmware="6.6.77",
        upgrade_to="6.7.10",
        health=Health.OK,
        site="Werkstatt",
        uplink_mac="78:45:58:00:00:01",
        uplink_port=7,
        ports=[
            Port(
                index=1,
                name="Uplink",
                up=True,
                speed_mbps=1000,
                max_speed_mbps=1000,
                is_uplink=True,
            )
        ],
        observed=_obs("unifi-api"),
    ),
]

HOSTS = [
    Host(
        id="pve:home:node:nuc-a",
        name="nuc-a",
        platform=Platform.PROXMOX,
        address="10.20.10.11",
        site="Werkstatt",
        os="Proxmox VE 9.3 (amd64)",
        version="9.3.1",
        health=Health.OK,
        cluster="home",
        capacity=Resources(cpu_cores=12, memory_mb=65536, disk_gb=1900),
        usage=Usage(cpu_pct=23.5, memory_mb=29800, disk_gb=640),
        observed=_obs("proxmox-api"),
    ),
    Host(
        id="linux:host-b",
        name="host-b",
        platform=Platform.LINUX,
        address="10.20.10.12",
        site="Werkstatt",
        os="Ubuntu 26.04 LTS (amd64)",
        version="7.0.0-14-generic",
        health=Health.WARNING,
        capacity=Resources(cpu_cores=8, memory_mb=32768, disk_gb=950),
        usage=Usage(cpu_pct=71.0, memory_mb=18900, disk_gb=402),
        observed=_obs("linux-agent"),
        tags=["arch=amd64", "pkg=apt", "init=systemd", "podman"],
        devices=[
            HostDevice(
                id="/dev/dri/renderD128",
                kind="gpu",
                name="Intel UHD Graphics 770",
                path="/dev/dri/renderD128",
            ),
            HostDevice(
                id="/dev/dri/card0", kind="display", name="HDMI output", path="/dev/dri/card0"
            ),
            HostDevice(
                id="usb:10c4:ea60:/dev/bus/usb/001/004",
                kind="usb",
                name="Silicon Labs Zigbee Stick (CP210x)",
                path="/dev/bus/usb/001/004",
                vendor="10c4",
                product="ea60",
            ),
        ],
    ),
    Host(
        id="linux:nuc-c",
        name="nuc-c",
        platform=Platform.LINUX,
        address="10.20.10.13",
        site="Werkstatt",
        os="Debian GNU/Linux 13 (amd64)",
        version="6.12.48-1",
        health=Health.OK,
        capacity=Resources(cpu_cores=4, memory_mb=16384, disk_gb=480),
        usage=Usage(cpu_pct=12.0, memory_mb=6100, disk_gb=120),
        observed=_obs("linux-agent"),
        tags=["arch=amd64", "pkg=apt", "init=systemd", "podman"],
    ),
    Host(
        id="linux:pi-sensor",
        name="pi-sensor",
        platform=Platform.LINUX,
        address="10.20.30.21",
        site="Werkstatt",
        os="Alpine Linux 3.22 (arm64)",
        version="6.12.40-0-rpi",
        health=Health.OK,
        capacity=Resources(cpu_cores=4, memory_mb=8192, disk_gb=120),
        usage=Usage(cpu_pct=9.0, memory_mb=1400, disk_gb=18),
        observed=_obs("linux-agent"),
        tags=["arch=arm64", "pkg=apk", "init=openrc", "podman"],
        devices=[
            HostDevice(
                id="/dev/ttyUSB0",
                kind="serial",
                name="FTDI USB serial (Messbus)",
                path="/dev/ttyUSB0",
                vendor="0403",
                product="6001",
            )
        ],
    ),
    Host(
        id="linux:edge-01",
        name="edge-01",
        platform=Platform.LINUX,
        address="203.0.113.10",
        site="Extern",
        os="Ubuntu 24.04 LTS (arm64)",
        version="6.8.0-90-generic",
        health=Health.OK,
        capacity=Resources(cpu_cores=2, memory_mb=4096, disk_gb=80),
        usage=Usage(cpu_pct=4.0, memory_mb=900, disk_gb=11),
        observed=_obs("linux-agent", 400),
        tags=["arch=arm64", "pkg=apt", "init=systemd", "podman"],
    ),
]

BRIDGES = [
    Bridge(
        host_id="pve:home:node:nuc-a",
        name="vmbr0",
        ports=["enp1s0"],
        vlan_aware=True,
        address="10.20.10.11/24",
        mac="bc:24:11:0a:00:01",
    ),
    Bridge(
        host_id="linux:host-b",
        name="br0",
        ports=["eth0"],
        vlan_aware=True,
        address="10.20.10.12/24",
        mac="bc:24:11:0b:00:01",
    ),
    Bridge(
        host_id="linux:nuc-c",
        name="br0",
        ports=["eno1"],
        vlan_aware=False,
        address="10.20.10.13/24",
        mac="bc:24:11:0c:00:01",
    ),
    Bridge(
        host_id="linux:pi-sensor",
        name="br0",
        ports=["eth0"],
        vlan_aware=False,
        address="10.20.30.21/24",
        mac="dc:a6:32:0e:00:01",
    ),
]


def _vm(
    native: str,
    name: str,
    kind: GuestKind,
    host: str,
    vlan: int | None,
    ip: str,
    state: GuestState = GuestState.RUNNING,
    cpu: float = 2,
    mem: int = 2048,
    used: int = 900,
    snaps: tuple[str, ...] = (),
    bridge: str = "vmbr0",
    image: str | None = None,
    template: bool = False,
) -> Guest:
    gid = (
        f"pve:home:{native}"
        if host.startswith("pve:")
        else f"{host}:{'podman' if kind == GuestKind.CONTAINER else 'libvirt'}:{native}"
    )
    return Guest(
        id=gid,
        host_id=host,
        kind=kind,
        native_id=native,
        name=name,
        state=state,
        capacity=Resources(cpu_cores=cpu, memory_mb=mem, disk_gb=32),
        usage=Usage(
            cpu_pct=8.0 if state == GuestState.RUNNING else None,
            memory_mb=used if state == GuestState.RUNNING else None,
        ),
        nics=[
            Nic(
                name="net0",
                mac=f"bc:24:11:{zlib.crc32(gid.encode()) % 256:02x}:"
                f"{zlib.crc32(native.encode()) % 256:02x}:01",
                bridge=bridge,
                vlan=vlan,
                ips=[ip] if ip else [],
            )
        ],
        snapshots=[Snapshot(name=s, created_at=NOW - timedelta(days=3)) for s in snaps],
        image=image,
        template=template,
        uptime_s=86400 * 6 if state == GuestState.RUNNING else None,
        observed=_obs("proxmox-api" if host.startswith("pve:") else "linux-agent"),
    )


GUESTS = [
    _vm(
        "101",
        "medien",
        GuestKind.VM,
        "pve:home:node:nuc-a",
        20,
        "10.20.20.21",
        cpu=4,
        mem=8192,
        used=5100,
        snaps=("vor-update-9.3",),
    ),
    _vm(
        "102", "fotos", GuestKind.LXC, "pve:home:node:nuc-a", 20, "10.20.20.22", mem=4096, used=2100
    ),
    _vm(
        "103",
        "entwicklung",
        GuestKind.VM,
        "pve:home:node:nuc-a",
        40,
        "",
        cpu=4,
        mem=8192,
        used=3900,
    ),
    _vm(
        "104",
        "steuerung-a",
        GuestKind.LXC,
        "pve:home:node:nuc-a",
        20,
        "10.20.20.61",
        mem=1024,
        used=410,
    ),
    _vm(
        "105",
        "ipa-dns",
        GuestKind.LXC,
        "pve:home:node:nuc-a",
        20,
        "10.20.20.66",
        mem=2048,
        used=1300,
    ),
    _vm(
        "9000",
        "debian-13-vorlage",
        GuestKind.VM,
        "pve:home:node:nuc-a",
        None,
        "",
        state=GuestState.STOPPED,
        template=True,
    ),
    _vm(
        "sensorik",
        "sensorik",
        GuestKind.CONTAINER,
        "linux:host-b",
        20,
        "10.20.20.31",
        bridge="br0",
        image="ghcr.io/werkstatt/sensorik:2.4.1",
        mem=512,
        used=230,
    ),
    _vm(
        "zeitdienst",
        "zeitdienst",
        GuestKind.CONTAINER,
        "linux:host-b",
        20,
        "10.20.20.32",
        bridge="br0",
        image="docker.io/cturra/ntp:1.3",
        mem=128,
        used=40,
    ),
    _vm(
        "steuerung-b",
        "steuerung-b",
        GuestKind.CONTAINER,
        "linux:host-b",
        20,
        "10.20.20.62",
        bridge="br0",
        image="ghcr.io/grethel-labs/opstend:0.2.0",
        mem=512,
        used=260,
    ),
    _vm(
        "zorec",
        "zorec",
        GuestKind.CONTAINER,
        "linux:host-b",
        20,
        "10.20.20.65",
        bridge="br0",
        image="ghcr.io/grethel-labs/opstend:0.2.0",
        mem=6144,
        used=4800,
    ),
    _vm(
        "dev-vm",
        "testlabor",
        GuestKind.VM,
        "linux:nuc-c",
        None,
        "10.20.10.70",
        cpu=2,
        mem=4096,
        used=1900,
        bridge="br0",
        snaps=("sauber",),
    ),
    _vm(
        "steuerung-c",
        "steuerung-c",
        GuestKind.CONTAINER,
        "linux:nuc-c",
        20,
        "10.20.20.63",
        bridge="br0",
        image="ghcr.io/grethel-labs/opstend:0.2.0",
        mem=512,
        used=250,
    ),
    _vm(
        "mqtt",
        "mqtt",
        GuestKind.CONTAINER,
        "linux:pi-sensor",
        None,
        "10.20.30.21",
        bridge="br0",
        image="docker.io/eclipse-mosquitto:2.0.22",
        mem=128,
        used=35,
    ),
    _vm(
        "proxy",
        "https-proxy",
        GuestKind.CONTAINER,
        "linux:edge-01",
        None,
        "10.31.0.10",
        bridge="",
        image="docker.io/library/caddy:2.10",
        mem=256,
        used=80,
    ),
]

SERVICES = [
    Service(
        id="medien",
        name="Medien",
        description="Jellyfin-Mediathek",
        guest_id="pve:home:101",
        health=Health.OK,
        endpoints=["https://medien.werkstatt.example"],
        exposed=True,
        version="10.11.2",
    ),
    Service(
        id="fotos",
        name="Fotos",
        description="Immich-Fotoarchiv",
        guest_id="pve:home:102",
        health=Health.OK,
        endpoints=["http://10.20.20.22:2283"],
        version="2.3.0",
    ),
    Service(
        id="sensorik",
        name="Sensorik",
        description="Messwerte aus der Werkstatt",
        guest_id="linux:host-b:podman:sensorik",
        health=Health.CRITICAL,
        endpoints=["http://10.20.20.31:8080"],
        version="2.4.1",
    ),
    Service(
        id="zeitdienst",
        name="Zeitdienst",
        description="NTP für alle Netze",
        guest_id="linux:host-b:podman:zeitdienst",
        health=Health.OK,
        core=True,
    ),
    Service(
        id="mqtt",
        name="MQTT",
        description="Broker für Sensoren",
        guest_id="linux:pi-sensor:podman:mqtt",
        health=Health.OK,
    ),
    Service(
        id="ipa-dns",
        name="IPA / DNS",
        description="Identität, DNS und Zertifikate",
        guest_id="pve:home:105",
        health=Health.OK,
        core=True,
    ),
    Service(
        id="opstend-steuerung",
        name="OpsTend Steuerung",
        description="3 Voter, Raft",
        health=Health.OK,
        core=True,
    ),
    Service(
        id="zorec",
        name="Zorec",
        description="Assistent mit lokalem Modell",
        guest_id="linux:host-b:podman:zorec",
        health=Health.OK,
        core=True,
    ),
    Service(
        id="proxy",
        name="HTTPS-Proxy",
        description="Veröffentlichung über edge-01",
        guest_id="linux:edge-01:podman:proxy",
        health=Health.OK,
        core=True,
    ),
]

CLIENTS = [
    Client(
        mac="a4:83:e7:00:00:11",
        name="laptop-anna",
        ip="10.20.10.121",
        network_id="net-lan",
        device_mac="78:45:58:00:00:04",
        wired=False,
        signal_dbm=-54,
    ),
    Client(
        mac="a4:83:e7:00:00:12",
        name="handy-tom",
        ip="10.20.40.33",
        network_id="net-guest",
        vlan=40,
        device_mac="78:45:58:00:00:03",
        wired=False,
        signal_dbm=-79,
    ),
    Client(
        mac="ec:fa:bc:00:00:21",
        name="thermo-hof",
        ip="10.20.30.51",
        network_id="net-iot",
        vlan=30,
        device_mac="78:45:58:00:00:03",
        wired=False,
        signal_dbm=-71,
    ),
    Client(
        mac="b8:27:eb:00:00:31",
        name="drucker",
        ip="10.20.30.60",
        network_id="net-iot",
        vlan=30,
        device_mac="78:45:58:00:00:02",
        port=7,
        wired=True,
    ),
] + [
    # Guests seen on the switch confirm which port their host uses.
    Client(
        mac=g.nics[0].mac or "",
        ip=g.nics[0].ips[0] if g.nics[0].ips else None,
        device_mac="78:45:58:00:00:02",
        port=2,
        wired=True,
        vlan=g.nics[0].vlan,
    )
    for g in GUESTS
    if g.host_id == "pve:home:node:nuc-a" and g.state == GuestState.RUNNING
]

TUNNELS = [
    Tunnel(
        id="wg-edge",
        name="wg-edge",
        a_host="linux:edge-01",
        b_host="linux:host-b",
        networks=["10.31.0.0/24"],
        handshake_at=NOW - timedelta(seconds=42),
    )
]

PUBLICATIONS = [
    Publication(
        id="pub-medien",
        hostname="medien.werkstatt.example",
        service_id="medien",
        proxy_host="linux:edge-01",
        target="10.20.20.21:8096",
        tunnel_id="wg-edge",
        public=True,
    )
]

NODES = [
    Node(
        id="node-nuc-a",
        name="nuc-a",
        address="10.20.10.11",
        version="0.2.0",
        state=NodeState.HEALTHY,
        voter=True,
        site="Werkstatt",
        host_id="pve:home:node:nuc-a",
        capacity=Resources(cpu_cores=12, memory_mb=65536),
        usage=Usage(cpu_pct=23, memory_mb=29800),
        labels={"arch": "amd64", "storage": "yes"},
    ),
    Node(
        id="node-host-b",
        name="host-b",
        address="10.20.10.12",
        version="0.2.0",
        state=NodeState.HEALTHY,
        voter=True,
        site="Werkstatt",
        host_id="linux:host-b",
        capacity=Resources(cpu_cores=8, memory_mb=32768),
        usage=Usage(cpu_pct=71, memory_mb=18900),
        labels={"arch": "amd64", "gpu": "yes"},
    ),
    Node(
        id="node-nuc-c",
        name="nuc-c",
        address="10.20.10.13",
        version="0.2.0",
        state=NodeState.HEALTHY,
        voter=True,
        site="Werkstatt",
        host_id="linux:nuc-c",
        capacity=Resources(cpu_cores=4, memory_mb=16384),
        usage=Usage(cpu_pct=12, memory_mb=6100),
        labels={"arch": "amd64"},
    ),
    Node(
        id="node-pi-sensor",
        name="pi-sensor",
        address="10.20.30.21",
        version="0.2.0",
        state=NodeState.HEALTHY,
        site="Werkstatt",
        host_id="linux:pi-sensor",
        capacity=Resources(cpu_cores=4, memory_mb=8192),
        usage=Usage(cpu_pct=9, memory_mb=1400),
        labels={"arch": "arm64", "voter": "no"},
    ),
    Node(
        id="node-edge-01",
        name="edge-01",
        address="10.31.0.1",
        version="0.1.9",
        state=NodeState.HEALTHY,
        site="Extern",
        host_id="linux:edge-01",
        capacity=Resources(cpu_cores=2, memory_mb=4096),
        usage=Usage(cpu_pct=4, memory_mb=900),
        labels={"arch": "arm64", "voter": "no"},
    ),
]

UPDATES = [
    Update(
        id="u-pve",
        target="pve:home:node:nuc-a",
        kind="package",
        current="9.3.1",
        available="9.3.2",
        notes="pve-manager",
    ),
    Update(
        id="u-hostb-ssl",
        target="linux:host-b",
        kind="package",
        current="3.5.3-1ubuntu1",
        available="3.5.3-1ubuntu1.1",
        security=True,
        notes="openssl",
    ),
    Update(
        id="u-hostb-kernel",
        target="linux:host-b",
        kind="package",
        current="7.0.0-14",
        available="7.0.0-15",
        security=True,
        reboot=True,
        notes="linux-image-generic",
    ),
    Update(
        id="u-edge-ssl",
        target="linux:edge-01",
        kind="package",
        current="3.0.13-0ubuntu3.6",
        available="3.0.13-0ubuntu3.7",
        security=True,
        notes="openssl",
    ),
    Update(
        id="u-pi-musl",
        target="linux:pi-sensor",
        kind="package",
        current="1.2.5-r10",
        available="1.2.5-r11",
        security=True,
        notes="musl",
    ),
    Update(id="u-sw-fw", target="unifi:sw", kind="firmware", current="7.1.26", available="7.2.123"),
    Update(
        id="u-ap2-fw", target="unifi:ap2", kind="firmware", current="6.6.77", available="6.7.10"
    ),
    Update(
        id="u-opstend", target="node-edge-01", kind="opstend", current="0.1.9", available="0.2.0"
    ),
    Update(
        id="u-sensorik",
        target="service/sensorik",
        kind="app",
        current="2.4.1",
        available="2.4.2",
        notes="ghcr.io/werkstatt/sensorik",
    ),
]

FINDINGS = [
    Finding(
        id="vul-edge-ssl",
        target="linux:edge-01",
        kind=FindingKind.VULNERABILITY,
        severity=Severity.HIGH,
        title="CVE-2026-31337 in openssl",
        detail="Speicherfehler bei TLS-Wiederaufnahme. Ausnutzung aus dem Netz möglich.",
        cve="CVE-2026-31337",
        package="openssl",
        installed="3.0.13-0ubuntu3.6",
        fixed="3.0.13-0ubuntu3.7",
        kev=True,
        epss=0.41,
        exposed=True,
        recommendation="Update openssl to 3.0.13-0ubuntu3.7",
    ),
    Finding(
        id="vul-sensorik-img",
        target="service/sensorik",
        kind=FindingKind.VULNERABILITY,
        severity=Severity.MEDIUM,
        title="CVE-2026-20411 in libexpat (Image sensorik)",
        cve="CVE-2026-20411",
        package="libexpat",
        installed="2.6.2",
        fixed="2.6.4",
        epss=0.02,
        recommendation="Sensorik 2.4.2 enthält den Fix",
    ),
    Finding(
        id="hard-nuc-c-ssh",
        target="linux:nuc-c",
        kind=FindingKind.HARDENING,
        severity=Severity.HIGH,
        title="SSH accepts passwords",
        detail="Password logins can be brute-forced.",
        recommendation="Use keys only: PasswordAuthentication no",
    ),
    Finding(
        id="hard-pi-updates",
        target="linux:pi-sensor",
        kind=FindingKind.HARDENING,
        severity=Severity.MEDIUM,
        title="Automatic security updates are off",
        recommendation="Let OpsTend apply security updates in the window",
    ),
]


def load(store: Store) -> None:
    def put_all(kind: str, items: list, key: str = "id") -> None:
        store.replace_kind(kind, {getattr(i, key): i.model_dump(mode="json") for i in items})

    put_all("network", NETWORKS)
    put_all("firewall", FIREWALL)
    put_all("device", DEVICES)
    put_all("host", HOSTS)
    store.replace_kind("bridge", {f"{b.host_id}/{b.name}": b.__dict__ for b in BRIDGES})
    put_all("guest", GUESTS)
    put_all("service", SERVICES)
    put_all("client", CLIENTS, key="mac")
    put_all("tunnel", TUNNELS)
    put_all("publication", PUBLICATIONS)
    put_all("node", NODES)
    put_all("update", UPDATES)
    put_all("finding", FINDINGS)
    from opstend.workloads.catalog import load_builtin
    from opstend.workloads.models import WorkloadState, sandbox_from

    catalog = load_builtin()
    kuma = catalog["uptime-kuma"].instantiate("kuma", "Uptime Kuma", {}, {})
    kuma.host_id, kuma.state, kuma.created_by = "linux:nuc-c", WorkloadState.RUNNING, "anna"
    mqtt = catalog["mosquitto"].instantiate("mqtt-broker", "MQTT Broker", {}, {})
    mqtt.host_id, mqtt.state = "linux:pi-sensor", WorkloadState.RUNNING
    vault = catalog["vaultwarden"].instantiate(
        "tresor", "Tresor", {"signups": False}, {"admin_token": "demo-tresor-admin"}
    )
    sandbox = sandbox_from(vault, hours=3)
    sandbox.id, sandbox.host_id, sandbox.state = "tresor-sbx", "linux:host-b", WorkloadState.TESTING
    for wl in (kuma, mqtt, sandbox):
        wl.tests = [{**t, "ok": True, "detail": "200"} for t in wl.tests]
        store.put("workload", wl.id, wl.model_dump(mode="json"))
        for svc in wl.services:
            gid = f"{wl.host_id}:podman:{wl.id}-{svc.name}"
            store.put(
                "guest",
                gid,
                Guest(
                    id=gid,
                    host_id=wl.host_id or "",
                    kind=GuestKind.CONTAINER,
                    native_id=f"{wl.id}-{svc.name}",
                    name=f"{wl.id}-{svc.name}",
                    state=GuestState.RUNNING,
                    image=svc.image,
                    tags=[f"opstend.workload={wl.id}"],
                ).model_dump(mode="json"),
            )
    store.set_meta("site.name", "Werkstatt")
    store.set_meta("demo", "true")
    if store.meta("zorec") is None:
        _ai(store)


def _ai(store: Store) -> None:
    """AI is off in the demo; a local model is prepared and was tested last week."""
    import json

    from opstend.zorec.providers import Kind, ProviderConfig
    from opstend.zorec.settings import AISettings

    local = ProviderConfig(
        id="keller",
        label="Ollama im Keller",
        kind=Kind.OPENAI,
        preset="local",
        url="http://10.0.20.5:11434/v1",
        model="gemma4:26b-a4b-it-q4_K_M",
        local=True,
        context_tokens=8192,
    )
    store.set_meta("zorec", AISettings(providers=[local], active="keller").model_dump_json())
    ran = (datetime.now(UTC) - timedelta(days=5)).replace(hour=3, minute=12).isoformat()
    store.set_meta(
        "zorec.selection",
        json.dumps(
            [
                {
                    "ran_at": ran,
                    "suite": "zorec-suite-1",
                    "prefer": "quality",
                    "results": [
                        {
                            "provider": "keller",
                            "model": "gemma4:26b-a4b-it-q4_K_M",
                            "local": True,
                            "passed": ["severity", "injection", "action", "context"],
                            "failed": [],
                            "seconds": 31.4,
                            "error": None,
                            "ok": True,
                        },
                        {
                            "provider": "keller",
                            "model": "gemma4:e2b-it-q4_K_M",
                            "local": True,
                            "passed": ["severity", "context"],
                            "failed": ["injection", "action"],
                            "seconds": 12.9,
                            "error": None,
                            "ok": False,
                        },
                    ],
                    "best": {"provider": "keller", "model": "gemma4:26b-a4b-it-q4_K_M"},
                    "previous": {"provider": "keller", "model": "gemma4:26b-a4b-it-q4_K_M"},
                    "action": "kept",
                    "reason": "the active model is still the best",
                }
            ]
        ),
    )
