"""Offline tests of the design (no NetBox needed). Run with: pytest -q

They protect the design rules: if someone edits the intent and breaks a rule,
CI turns red before anything reaches NetBox.
"""

import ipaddress
from collections import Counter

import pytest

from nblab.design import build, expand_ports


@pytest.fixture(scope="module")
def d():
    return build()


def test_size(d):
    assert len(d.devices) == 50
    assert len(d.vlans) >= 60


def test_unique_ips(d):
    plain = Counter((ip.address.split("/")[0], ip.vrf) for ip in d.ips if ip.role != "vip")
    assert [k for k, c in plain.items() if c > 1] == []


def test_ips_inside_prefixes(d):
    nets = [(ipaddress.ip_network(p.prefix), p.vrf) for p in d.prefixes]
    for ip in d.ips:
        addr = ipaddress.ip_interface(ip.address).ip
        assert any(addr in n and v == ip.vrf for n, v in nets), ip


def test_every_access_switch_dual_homed(d):
    for dev in (x for x in d.devices.values() if x.role == "access"):
        ends = [c for c in d.cables if dev.name in (c.a[0], c.b[0])]
        peers = {c.b[0] if c.a[0] == dev.name else c.a[0] for c in ends}
        assert len(ends) == 2 and len(peers) == 2, dev.name


def test_port_used_once(d):
    ends = Counter(e for c in d.cables for e in (c.a, c.b))
    assert [e for e, n in ends.items() if n > 1] == []


def test_mclag_symmetric(d):
    agg = sorted(x.name for x in d.devices.values() if x.role == "agg")
    lags = [{i.name: (i.mode, i.untagged, i.tagged) for i in d.device_interfaces(a)
             if i.kind == "lag"} for a in agg]
    assert lags[0] == lags[1]


def test_access_vlans_have_gateway(d):
    agg = [x.name for x in d.devices.values() if x.role == "agg"]
    for dev in (x for x in d.devices.values() if x.role == "access"):
        for vid in d.interfaces[(dev.name, "lag1")].tagged:
            for a in agg:
                assert (a, f"vlan{vid}") in d.interfaces, (dev.name, vid)


def test_edge_vlans_are_on_uplink(d):
    """A VLAN used on an edge port must be carried on the uplink (except parking)."""
    for dev in (x for x in d.devices.values() if x.role == "access"):
        trunk = set(d.interfaces[(dev.name, "lag1")].tagged)
        for i in d.device_interfaces(dev.name):
            if i.kind == "physical" and i.mode and i.enabled:
                used = set(i.tagged) | {i.untagged}
                assert used <= trunk, (dev.name, i.name, used - trunk)


def test_expand_ports():
    assert expand_ports("1-3,7") == [1, 2, 3, 7]
