"""Validate the data in NetBox BEFORE any config is generated.

Each rule protects against a real mistake that would break the network:
a trunk with different VLANs on both ends, an access switch with only one
uplink, a VLAN without a gateway, a VSX pair with different MC-LAG settings...
"""

import ipaddress
import re
from collections import Counter

from .snapshot import Snapshot


class Result:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, rule, msg):
        self.errors.append((rule, msg))

    def warn(self, rule, msg):
        self.warnings.append((rule, msg))


def run_checks(nb, snap=None, quiet=False):
    s = snap or Snapshot(nb)
    r = Result()
    site = s.site.name

    switches = [d for d in s.devices if s.is_switch(d)]
    access = [d for d in switches if s.role(d) == "access"]
    agg = [d for d in switches if s.role(d) == "agg"]

    # R1 naming convention
    pat = re.compile(rf"^{site}-((CORE|AGG|FW)-\d{{2}}|F\d+-ACC-\d{{2}})$")
    for d in s.devices:
        if not pat.match(d):
            r.error("naming", f"{d} does not follow <SITE>-CORE-01 / <SITE>-F1-ACC-01")

    # R2 basic device data
    for d, rec in s.devices.items():
        if rec.primary_ip4 is None:
            r.error("device", f"{d} has no primary IPv4 (management)")
        if rec.location is None:
            r.warn("device", f"{d} has no location (floor / room)")
        if s.role(d) in ("core", "agg", "access") and not s.is_switch(d):
            r.error("device", f"{d} has no platform '{'aruba-aoscx'}', templates cannot be chosen")

    # R3 access switches: dual-homed MC-LAG uplink to the agg VSX pair
    for d in access:
        if (d, "lag1") not in s.ifaces:
            r.error("uplink", f"{d} has no lag1 uplink")
            continue
        members = s.members(d, "lag1")
        if len(members) != 2:
            r.error("uplink", f"{d} lag1 has {len(members)} member(s), expected 2")
        peers = [s.peer(d, m) for m in members]
        if None in peers:
            r.error("uplink", f"{d} lag1 member without cable: {members}")
            continue
        peer_devs = {p[0] for p in peers}
        if len(peer_devs) != 2 or not peer_devs <= set(agg):
            r.error("uplink", f"{d} uplinks must go to two different AGG switches, got {sorted(peer_devs)}")

    # R4 every cable between two switches: same L2 settings or matching /31
    seen = set()
    for (d, name), i in s.ifaces.items():
        if not s.is_switch(d) or i.cable is None:
            continue
        p = s.peer(d, name)
        if p is None or p in seen or not s.is_switch(p[0]):
            continue
        seen.add((d, name))
        a, b = s.l2(d, name), s.l2(*p)
        if a != b:
            r.error("trunk", f"{d} {name} <-> {p[0]} {p[1]}: VLAN settings differ "
                             f"({_fmt(a)} vs {_fmt(b)})")
        ipa, ipb = s.addresses(d, name), s.addresses(*p)
        if ipa or ipb:
            if not (ipa and ipb) or s.iface_net(ipa[0]) != s.iface_net(ipb[0]):
                r.error("p2p", f"{d} {name} <-> {p[0]} {p[1]}: routed link IPs not in the same subnet")
        if s.mtu(d, name) != s.mtu(*p):
            r.error("mtu", f"{d} {name} <-> {p[0]} {p[1]}: MTU differs "
                           f"({s.mtu(d, name)} vs {s.mtu(*p)})")
        if not i.enabled or not s.ifaces[p].enabled:
            r.warn("cable", f"{d} {name} <-> {p[0]} {p[1]}: cabled but one side is disabled")

    # R4b on AOS-CX, MTU of a LAG is configured on the LAG, never on its members
    for (d, name), i in s.ifaces.items():
        if s.is_switch(d) and i.lag is not None and i.mtu:
            r.error("mtu", f"{d} {name} is a member of {i.lag.name}; set the MTU on the LAG, not the port")

    # R5 VSX pairs: context, ISL, keepalive, identical MC-LAGs
    for d in switches:
        vsx = s.context(d).get("vsx")
        if s.role(d) in ("core", "agg") and not vsx:
            r.error("vsx", f"{d} has no 'vsx' in its config context")
            continue
        if not vsx:
            continue
        peer = vsx.get("peer")
        if peer not in s.devices:
            r.error("vsx", f"{d} VSX peer '{peer}' does not exist")
            continue
        pv = s.context(peer).get("vsx", {})
        if d < peer:
            if pv.get("system_mac") != vsx.get("system_mac"):
                r.error("vsx", f"{d}/{peer}: different VSX system-mac")
            if pv.get("role") == vsx.get("role"):
                r.error("vsx", f"{d}/{peer}: both have VSX role '{vsx.get('role')}'")
            for dev in (d, peer):
                if len(s.members(dev, "lag256")) < 2:
                    r.error("vsx", f"{dev}: ISL lag256 needs at least 2 members")
            ka = [ip for dev in (d, peer) for i in s.by_device[dev]
                  for ip in s.addresses(dev, i.name) if i.vrf and i.vrf.name == "KA"]
            if len(ka) != 2 or s.iface_net(ka[0]) != s.iface_net(ka[1]):
                r.error("vsx", f"{d}/{peer}: keepalive needs one IP each in VRF KA, same /31")
            lags_a = {i.name for i in s.by_device[d] if i.type.value == "lag" and i.name != "lag256"}
            lags_b = {i.name for i in s.by_device[peer] if i.type.value == "lag" and i.name != "lag256"}
            for lag in sorted(lags_a ^ lags_b):
                r.error("mclag", f"{d}/{peer}: {lag} exists on only one VSX member")
            for lag in sorted(lags_a & lags_b):
                if s.l2(d, lag) != s.l2(peer, lag):
                    r.error("mclag", f"{d}/{peer}: {lag} has different VLANs on the two members")
                if s.members(d, lag) != s.members(peer, lag):
                    r.warn("mclag", f"{d}/{peer}: {lag} uses different member ports")

    # R6 every VLAN carried to access switches has a gateway on BOTH agg switches
    carried = set()
    for d in access:
        if (d, "lag1") in s.ifaces:
            mode, untagged, tagged = s.l2(d, "lag1")
            carried |= set(tagged) | ({untagged} if untagged else set())
    for vid in sorted(carried):
        vips = set()
        for a in agg:
            if (a, f"vlan{vid}") not in s.ifaces:
                r.error("gateway", f"VLAN {vid} is trunked to access but {a} has no interface vlan{vid}")
                continue
            if not s.addresses(a, f"vlan{vid}"):
                r.error("gateway", f"{a} vlan{vid} has no IP address")
            vips |= {str(ip.address) for ip in s.addresses(a, f"vlan{vid}", vip=True)}
        if len(vips) != 1:
            r.error("gateway", f"VLAN {vid}: expected exactly one VIP (active-gateway), found {sorted(vips)}")

    # R7 every VLAN used on an interface belongs to this site's VLAN group
    for (d, name), i in s.ifaces.items():
        for v in ([i.untagged_vlan] if i.untagged_vlan else []) + list(i.tagged_vlans or []):
            if v.vid not in s.vlans or s.vlans[v.vid].id != v.id:
                r.error("vlan", f"{d} {name} uses VLAN {v.vid} from outside the {site} VLAN group")

    # R8 VLANs with a gateway must have a prefix
    pfx_vlans = {p.vlan.vid for p in s.prefixes if p.vlan}
    for vid, v in s.vlans.items():
        has_svi = any((a, f"vlan{vid}") in s.ifaces for a in agg)
        if has_svi and vid not in pfx_vlans:
            r.error("prefix", f"VLAN {vid} {v.name} has an SVI but no prefix")

    # R9 duplicate IPs (VIPs are allowed to repeat)
    count = Counter()
    for (d, name), ips in s.ips.items():
        for ip in ips:
            if not (ip.role and ip.role.value == "vip"):
                count[(str(ip.address).split("/")[0], ip.vrf.name if ip.vrf else None)] += 1
    for (addr, vrf), c in count.items():
        if c > 1:
            r.error("ip", f"{addr} (vrf {vrf or 'global'}) is used {c} times")

    # R10 IPs must sit inside a prefix of the site
    nets = [(ipaddress.ip_network(str(p.prefix)), p.vrf.name if p.vrf else None) for p in s.prefixes]
    for (d, name), ips in s.ips.items():
        for ip in ips:
            a = ipaddress.ip_interface(str(ip.address)).ip
            vrf = ip.vrf.name if ip.vrf else None
            if not any(a in n and v == vrf for n, v in nets):
                r.warn("ip", f"{d} {name} {ip.address} is not inside any prefix of {site}")

    if not quiet:
        _print(r)
    return not r.errors


def _fmt(l2):
    mode, untagged, tagged = l2
    if mode is None:
        return "routed/none"
    return f"{mode} untagged={untagged} tagged={_ranges(tagged)}"


def _ranges(vids):
    from .render import vlan_ranges
    return vlan_ranges(vids) or "-"


def _print(r):
    for rule, msg in r.errors:
        print(f"  ERROR [{rule}] {msg}")
    for rule, msg in r.warnings:
        print(f"  WARN  [{rule}] {msg}")
    print(f"\nCheck finished: {len(r.errors)} error(s), {len(r.warnings)} warning(s).")
