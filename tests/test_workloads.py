from datetime import UTC, datetime

import pytest

from opstend.core.models import Host, HostDevice, Platform, Resources, Usage
from opstend.secrets import DataKey, open_sealed, seal_for
from opstend.workloads.catalog import load_builtin
from opstend.workloads.models import (
    Connection,
    DeviceKind,
    DeviceRequest,
    Egress,
    Environment,
    NetworkPolicy,
    promote,
    sandbox_from,
)
from opstend.workloads.plan import (
    candidates,
    isolation_summary,
    match_devices,
    parse_udev,
    podman_plan,
    removal_plan,
)

CATALOG = load_builtin()


def host(i, mem=16384, devices=(), arch="amd64", platform=Platform.LINUX):
    return Host(
        id=f"linux:h{i}",
        name=f"h{i}",
        platform=platform,
        address=f"10.0.0.{i}",
        capacity=Resources(cpu_cores=8, memory_mb=mem),
        usage=Usage(memory_mb=1000),
        tags=[f"arch={arch}"],
        devices=list(devices),
    )


def test_catalog_adapters_instantiate():
    assert {"jellyfin", "vaultwarden", "paperless", "whoami"} <= set(CATALOG)
    w = CATALOG["vaultwarden"].instantiate(
        "vault", "Tresor", {"signups": True}, {"admin_token": "s1"}
    )
    env = {e.name: e for e in w.services[0].env}
    assert env["SIGNUPS_ALLOWED"].value == "true" and env["ADMIN_TOKEN"].secret
    assert w.secrets == {"ADMIN_TOKEN": "s1"}
    with pytest.raises(ValueError):
        CATALOG["vaultwarden"].instantiate("vault", "Tresor", {}, {})
    mq = CATALOG["mosquitto"].instantiate("mqtt", "MQTT", {}, {})
    assert "allow_anonymous false" in mq.files[0]["content"]


def test_internal_workload_gets_internal_network_and_ingress_forwarder():
    w = CATALOG["vaultwarden"].instantiate("vault", "Tresor", {}, {"admin_token": "s1"})
    plan = podman_plan(w, "10.0.0.5", [])
    argvs = [c.argv for c in plan]
    assert [
        "podman",
        "network",
        "create",
        "--label",
        "opstend.workload=vault",
        "--internal",
        "opstend-wl-vault",
    ] in argvs
    run = next(a for a in argvs if a[:2] == ["podman", "run"] and "vault-app" in a)
    assert "--cap-drop=ALL" in run and "--userns=auto" in run and "--publish" not in run
    assert any("ADMIN_TOKEN" in x for x in run)
    forwarder = next(a for a in argvs if "vault-in-80" in a)
    assert "10.0.0.5:80:80/tcp" in forwarder and "TCP:app:80" in forwarder
    secret = next(c for c in plan if c.stdin_secret)
    assert secret.stdin_secret == "s1" and "s1" not in " ".join(secret.argv)


def test_allowlist_egress_uses_proxy_and_internet_egress_publishes_directly():
    w = CATALOG["jellyfin"].instantiate("media", "Medien", {}, {})
    plan = podman_plan(w, "10.0.0.5", [])
    proxy_cfg = next(c for c in plan if c.stdin_text and "dstdomain" in c.stdin_text)
    assert "api.themoviedb.org" in proxy_cfg.stdin_text
    server = next(c.argv for c in plan if "media-server" in c.argv and c.argv[1] == "run")
    assert "HTTPS_PROXY=http://egress:3128" in server
    kuma = CATALOG["uptime-kuma"].instantiate("kuma", "Kuma", {}, {})
    run = next(c.argv for c in podman_plan(kuma, "10.0.0.5", []) if "kuma-app" in c.argv)
    assert "10.0.0.5:3001:3001/tcp" in run
    create = podman_plan(kuma, "10.0.0.5", [])[0].argv
    assert "isolate=true" in create and "--internal" not in create


def test_connections_use_dedicated_link_networks():
    w = CATALOG["whoami"].instantiate("web", "Web", {}, {})
    w.connections = [
        Connection(to_workload="mqtt", to_service="broker", port=1883, reason="metrics")
    ]
    argvs = [c.argv for c in podman_plan(w, "10.0.0.5", [])]
    assert [
        "podman",
        "network",
        "create",
        "--internal",
        "--label",
        "opstend.workload=web",
        "opstend-link-web-mqtt",
    ] in argvs
    assert [
        "podman",
        "network",
        "connect",
        "--alias",
        "mqtt-broker",
        "opstend-link-web-mqtt",
        "mqtt-broker",
    ] in argvs
    assert "isolation" not in str(removal_plan(w))
    assert any("opstend-link-web-mqtt" in c.argv for c in removal_plan(w))


def test_devices_are_matched_and_required_ones_block_placement():
    gpu = HostDevice(
        id="/dev/dri/renderD128", kind="gpu", name="Intel UHD", path="/dev/dri/renderD128"
    )
    stick = HostDevice(
        id="usb:10c4:ea60",
        kind="usb",
        name="Zigbee",
        path="/dev/bus/usb/001/004",
        vendor="10c4",
        product="ea60",
    )
    chosen, missing = match_devices(
        [DeviceRequest(kind=DeviceKind.USB, selector="10c4:ea60")], [gpu, stick], "ha"
    )
    assert chosen == [stick] and not missing
    w = CATALOG["home-assistant"].instantiate("ha", "HA", {}, {})
    w.devices = [DeviceRequest(kind=DeviceKind.USB, selector="10c4:ea60")]
    ranked = candidates(
        w,
        [host(1), host(2, devices=[stick]), host(3, arch="riscv64")],
        [],
        {},
        architectures=["amd64", "arm64"],
    )
    assert ranked[0].host_id == "linux:h2" and ranked[0].fits
    rejected = {c.host_id: c.reasons for c in ranked if not c.fits}
    assert "linux:h1" in rejected and any("riscv64" in r for r in rejected["linux:h3"])
    run = next(c.argv for c in podman_plan(w, "10.0.0.5", [stick]) if "ha-core" in c.argv)
    assert "/dev/bus/usb/001/004:/dev/bus/usb/001/004:rwm" in run


def test_udev_parsing():
    text = (
        "P: /devices/pci0000:00/0000:00:02.0/drm/renderD128\nE: DEVNAME=/dev/dri/renderD128\n"
        "E: SUBSYSTEM=drm\n\n"
        "P: /devices/usb1/1-2\nE: DEVNAME=/dev/bus/usb/001/004\nE: SUBSYSTEM=usb\n"
        "E: DEVTYPE=usb_device\nE: ID_VENDOR_ID=10c4\nE: ID_MODEL_ID=ea60\n"
        "E: ID_VENDOR_FROM_DATABASE=Silicon Labs\nE: ID_MODEL_FROM_DATABASE=CP210x UART Bridge\n"
        "E: ID_USB_INTERFACES=:ff0000:\n\n"
        "P: /devices/.../ttyUSB0\nE: DEVNAME=/dev/ttyUSB0\nE: SUBSYSTEM=tty\n"
    )
    kinds = {d.kind: d for d in parse_udev(text)}
    assert set(kinds) == {"gpu", "usb", "serial"}
    assert kinds["usb"].name == "Silicon Labs CP210x UART Bridge"


def test_sandbox_and_promotion():
    w = CATALOG["uptime-kuma"].instantiate("kuma", "Kuma", {}, {})
    sbx = sandbox_from(w, hours=2)
    assert sbx.environment == Environment.SANDBOX and sbx.network.egress == Egress.NONE
    assert sbx.id == "kuma-sbx" and sbx.expires_at > datetime.now(UTC)
    assert any("Sandbox" in line["text"] for line in isolation_summary(sbx))
    prod = promote(sbx)
    assert prod.id == "kuma" and prod.environment == Environment.PRODUCTION


def test_network_policy_validates_hosts():
    NetworkPolicy(egress=Egress.ALLOWLIST, egress_hosts=["api.example.org", "*.cdn.example.org"])
    with pytest.raises(ValueError):
        NetworkPolicy(egress=Egress.ALLOWLIST, egress_hosts=["http://evil/"])


def test_data_key_and_sealed_transport():
    key = DataKey.generate()
    sealed = key.encrypt("hunter2", "secret:a")
    assert key.decrypt(sealed, "secret:a") == "hunter2"
    with pytest.raises(Exception):  # noqa: B017 - bound to the id
        key.decrypt(sealed, "secret:b")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    node = ec.generate_private_key(ec.SECP256R1())
    pem = node.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert open_sealed(node, seal_for(pem, key.key)) == key.key
