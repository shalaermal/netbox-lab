"""netbox-lab: Build site / Validate site / Reset site (NetBox Custom Scripts).

GENERATED FILE - DO NOT EDIT. Edit nblab/*.py, intent/*.yml, devicetypes/*.yaml
or netbox/scripts/src/*.py, then run:  python tools/bundle_scripts.py
"""


# ===========================================================================
# Design engine (from nblab/design.py)
# ===========================================================================
import ipaddress
import re
from dataclasses import dataclass, field



class DesignError(Exception):
    pass


# --------------------------------------------------------------------------- model

@dataclass
class Vlan:
    vid: int
    name: str
    role: str | None = None
    description: str = ""


@dataclass
class Prefix:
    prefix: str
    role: str
    description: str
    vid: int | None = None
    vrf: str | None = None
    status: str = "active"          # "container" for supernets / infra ranges


@dataclass
class Device:
    name: str
    role: str                       # core | agg | access | firewall
    model: str                      # device type slug
    location: str                   # location slug
    index: int = 0                  # 1, 2 ... inside its role (access: global index)
    floor: int | None = None
    context: dict = field(default_factory=dict)   # NetBox local config context
    primary: tuple | None = None    # (interface name, address)


@dataclass
class Interface:
    device: str
    name: str
    kind: str = "physical"          # physical | lag | virtual
    enabled: bool = True
    description: str = ""
    mode: str | None = None         # None (routed / unset) | access | tagged | tagged-all
    untagged: int | None = None
    tagged: tuple = ()
    lag: str | None = None          # parent LAG name for members
    vrf: str | None = None
    mtu: int | None = None
    mgmt_only: bool = False


@dataclass
class IP:
    address: str
    device: str
    interface: str
    vrf: str | None = None
    role: str | None = None         # None | loopback | vip
    dns_name: str = ""
    description: str = ""


@dataclass
class Cable:
    a: tuple                        # (device, interface)
    b: tuple
    type: str
    label: str = ""

    @property
    def key(self):
        return frozenset([self.a, self.b])


@dataclass
class Design:
    site: dict
    standards: dict
    locations: list = field(default_factory=list)       # (slug, name, description)
    vlans: dict = field(default_factory=dict)           # vid -> Vlan
    prefixes: list = field(default_factory=list)
    devices: dict = field(default_factory=dict)         # name -> Device
    interfaces: dict = field(default_factory=dict)      # (device, name) -> Interface
    ips: list = field(default_factory=list)
    cables: list = field(default_factory=list)
    vrfs: tuple = ("mgmt", "KA")

    # helpers
    def add_iface(self, iface):
        key = (iface.device, iface.name)
        if key in self.interfaces:
            raise DesignError(f"interface defined twice: {key}")
        self.interfaces[key] = iface
        return iface

    def device_interfaces(self, device):
        return [i for (d, _), i in self.interfaces.items() if d == device]


# --------------------------------------------------------------------------- helpers

def vlan_ranges(vids):
    """[20, 21, 22, 23, 40] -> '20-23,40'"""
    vids = sorted(set(int(v) for v in vids))
    out, i = [], 0
    while i < len(vids):
        j = i
        while j + 1 < len(vids) and vids[j + 1] == vids[j] + 1:
            j += 1
        out.append(str(vids[i]) if i == j else f"{vids[i]}-{vids[j]}")
        i = j + 1
    return ",".join(out)


def expand_ports(spec):
    """'1-36' -> [1..36], '41,43-44' -> [41, 43, 44]"""
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def net(n, octet, host=None, plen=24):
    if host is None:
        return f"10.{n}.{octet}.0/{plen}"
    return f"10.{n}.{octet}.{host}/{plen}"


# --------------------------------------------------------------------------- build

def build(intent, dt_ports):
    """intent: dict with keys site, vlans, profiles, standards (see intent/*.yml).
    dt_ports: {device type slug: [interface names]} of the models used."""
    s = intent["site"]
    vp = intent["vlans"]
    pp = intent["profiles"]
    site = s["site"]
    n = int(site["number"])
    sname = site["name"]
    domain = site.get("domain", "lab.local")
    ports = s["ports"]
    # standards become a NetBox config context; add the few design facts the
    # config template needs and cannot derive on its own
    standards = dict(intent["standards"])
    standards["access_mgmt_vlan"] = int(vp["infra"]["access_mgmt_vlan"])
    standards.setdefault("timezone", site.get("timezone", "Europe/Berlin").lower())
    d = Design(site=site, standards=standards)

    # ---- locations
    d.locations.append((f"{site['slug']}-mdf", "MDF", "Main distribution room"))
    for f in s["floors"]:
        d.locations.append((f"{site['slug']}-f{f['floor']}", f"Floor {f['floor']}", ""))

    # ---- VLANs
    floors = [f["floor"] for f in s["floors"]]
    floor_vid = {}                                  # (floor, function name) -> vid
    for f in floors:
        for fn in vp["floor_functions"]:
            vid = 100 + 10 * f + int(fn["offset"])
            floor_vid[(f, fn["name"])] = vid
            d.vlans[vid] = Vlan(vid, f"F{f}-{fn['name']}", fn["role"], f"Floor {f} {fn['name']}")
    for sv in vp["site_vlans"]:
        d.vlans[sv["vid"]] = Vlan(sv["vid"], sv["name"], sv["role"], "Site-wide")
    park = vp["parking_vlan"]
    d.vlans[park["vid"]] = Vlan(park["vid"], park["name"], None, "Unused ports, no gateway")
    tr = vp["firewall_transit"]
    d.vlans[tr["vid"]] = Vlan(tr["vid"], tr["name"], "transit", "Core <-> firewall")

    gateway_vids = sorted(v for v in d.vlans if v not in (park["vid"], tr["vid"]))
    for vid in gateway_vids:
        if not 1 <= vid <= 254:
            raise DesignError(f"VLAN {vid} cannot be used as 3rd octet (rule vid = octet)")
    access_site_vids = [sv["vid"] for sv in vp["site_vlans"] if sv.get("on_access")]

    # ---- prefixes
    infra = vp["infra"]
    d.prefixes.append(Prefix(f"10.{n}.0.0/16", "infra", f"{sname} supernet", status="container"))
    for vid in gateway_vids:
        v = d.vlans[vid]
        d.prefixes.append(Prefix(net(n, vid), v.role, v.name, vid=vid))
    d.prefixes.append(Prefix(net(n, tr["third_octet"], 0, 29), "transit", tr["name"], vid=tr["vid"]))
    d.prefixes.append(Prefix(net(n, infra["p2p"]), "p2p", "Routed links core-agg (/31)", status="container"))
    d.prefixes.append(Prefix(net(n, infra["keepalive"]), "p2p", "VSX keepalive (/31)", vrf="KA", status="container"))
    d.prefixes.append(Prefix(net(n, infra["oob"]), "mgmt", "Out-of-band management", vrf="mgmt"))
    d.prefixes.append(Prefix(net(n, infra["loopback"]), "loopback", "Loopbacks (/32)", status="container"))

    # ---- devices
    mdf = f"{site['slug']}-mdf"
    vsx = s["vsx"]
    for role in ("core", "agg"):
        for i in (1, 2):
            name = f"{sname}-{role.upper()}-{i:02d}"
            peer = f"{sname}-{role.upper()}-{3 - i:02d}"
            d.devices[name] = Device(
                name, role, s["models"][role], mdf, index=i,
                context={"vsx": {"role": "primary" if i == 1 else "secondary",
                                 "system_mac": vsx[role]["system_mac"], "peer": peer}})
    for i in (1, 2):
        name = f"{sname}-FW-{i:02d}"
        d.devices[name] = Device(name, "firewall", s["models"]["firewall"], mdf, index=i)

    access = []                                     # in order: global index 1..N
    g = 0
    for f in s["floors"]:
        for k in range(1, int(f["access_switches"]) + 1):
            g += 1
            name = f"{sname}-F{f['floor']}-ACC-{k:02d}"
            dev = Device(name, "access", s["models"]["access"],
                         f"{site['slug']}-f{f['floor']}", index=g, floor=f["floor"],
                         context={"floor": f["floor"]})
            d.devices[name] = dev
            access.append(dev)

    for dev in d.devices.values():
        if dev.model not in dt_ports:
            raise DesignError(f"{dev.name}: no device type file for '{dev.model}' in devicetypes/")

    def check_port(dev_name, port):
        model = d.devices[dev_name].model
        if port not in dt_ports[model]:
            raise DesignError(f"{dev_name}: port {port} does not exist on {model}")
        return port

    core = [f"{sname}-CORE-01", f"{sname}-CORE-02"]
    agg = [f"{sname}-AGG-01", f"{sname}-AGG-02"]
    fws = [f"{sname}-FW-01", f"{sname}-FW-02"]
    mtu = d.standards.get("mtu_infra")

    # ---- VSX ISL + keepalive (core and agg pairs)
    ka_net = ipaddress.ip_network(net(n, infra["keepalive"]))
    for p, pair in enumerate((core, agg)):
        ka = list(ka_net.subnets(new_prefix=31))[p]
        for side, dev in enumerate(pair):
            d.add_iface(Interface(dev, "lag256", "lag", description="VSX ISL", mode="tagged-all",
                                  mtu=mtu))
            for port in ports["vsx_isl"]:
                d.add_iface(Interface(dev, check_port(dev, port), description="VSX ISL member",
                                      lag="lag256"))
            kp = check_port(dev, ports["vsx_keepalive"])
            d.add_iface(Interface(dev, kp, description="VSX keepalive", vrf="KA"))
            d.ips.append(IP(f"{ka[side]}/31", dev, kp, vrf="KA", description="VSX keepalive"))
        for port in ports["vsx_isl"]:
            d.cables.append(Cable((pair[0], port), (pair[1], port), "dac-passive", "VSX ISL"))
        d.cables.append(Cable((pair[0], ports["vsx_keepalive"]), (pair[1], ports["vsx_keepalive"]),
                              "dac-passive", "VSX keepalive"))

    # ---- loopbacks and OOB management (core, agg, firewall)
    host = {core[0]: 1, core[1]: 2, agg[0]: 11, agg[1]: 12}
    for dev, h in host.items():
        d.add_iface(Interface(dev, "loopback0", "virtual", description="Router ID"))
        d.ips.append(IP(net(n, infra["loopback"], h, 32), dev, "loopback0", role="loopback",
                        dns_name=f"{dev.lower()}-lo0.{domain}"))
    oob = {core[0]: 11, core[1]: 12, agg[0]: 21, agg[1]: 22, fws[0]: 31, fws[1]: 32}
    for dev, h in oob.items():
        check_port(dev, "mgmt")
        d.add_iface(Interface(dev, "mgmt", description="OOB management", vrf="mgmt", mgmt_only=True))
        addr = net(n, infra["oob"], h)
        d.ips.append(IP(addr, dev, "mgmt", vrf="mgmt", dns_name=f"{dev.lower()}.{domain}"))
        d.devices[dev].primary = ("mgmt", addr)

    # ---- routed core <-> agg links (/31, OSPF)
    p2p_net = ipaddress.ip_network(net(n, infra["p2p"]))
    p2p = list(p2p_net.subnets(new_prefix=31))
    links = [(core[0], ports["core_downlinks"][0], agg[0], ports["agg_uplinks"][0]),
             (core[0], ports["core_downlinks"][1], agg[1], ports["agg_uplinks"][0]),
             (core[1], ports["core_downlinks"][0], agg[0], ports["agg_uplinks"][1]),
             (core[1], ports["core_downlinks"][1], agg[1], ports["agg_uplinks"][1])]
    for i, (c, cp, a, ap) in enumerate(links):
        sub = p2p[i]
        for dev, port, ip, peer in ((c, cp, sub[0], a), (a, ap, sub[1], c)):
            d.add_iface(Interface(dev, check_port(dev, port), description=f"P2P to {peer}", mtu=mtu))
            d.ips.append(IP(f"{ip}/31", dev, port, description=f"P2P to {peer}"))
        d.cables.append(Cable((c, cp), (a, ap), "smf", "core-agg"))

    # ---- firewall pair <-> core pair (MC-LAG on the core side, VLAN 900)
    tvid = tr["vid"]
    for f_i, fw in enumerate(fws):
        lag_id = 101 + f_i
        d.add_iface(Interface(fw, "lag1", "lag", description="To core VSX"))
        for c_i, c in enumerate(core):
            cport = check_port(c, ports["core_firewall"][f_i])
            fport = check_port(fw, ports["firewall_ports"][c_i])
            if (c, f"lag{lag_id}") not in d.interfaces:
                d.add_iface(Interface(c, f"lag{lag_id}", "lag", description=f"MC-LAG to {fw}",
                                      mode="access", untagged=tvid))
            d.add_iface(Interface(c, cport, description=f"To {fw} {fport}", lag=f"lag{lag_id}"))
            d.add_iface(Interface(fw, fport, description=f"To {c} {cport}", lag="lag1"))
            d.cables.append(Cable((c, cport), (fw, fport), "mmf-om4", "core-firewall"))
        d.ips.append(IP(net(n, tr["third_octet"], 1, 29), fw, "lag1", role="vip",
                        description="Firewall HA cluster IP"))
    for i, c in enumerate(core):
        d.add_iface(Interface(c, f"vlan{tvid}", "virtual", description=tr["name"]))
        d.ips.append(IP(net(n, tr["third_octet"], 2 + i, 29), c, f"vlan{tvid}"))
        d.ips.append(IP(net(n, tr["third_octet"], 4, 29), c, f"vlan{tvid}", role="vip",
                        description="Core active-gateway (firewall return route)"))

    # ---- SVIs on the agg pair (VSX active-gateway): .1 VIP, .2 AGG-01, .3 AGG-02
    for vid in gateway_vids:
        v = d.vlans[vid]
        for i, a in enumerate(agg):
            d.add_iface(Interface(a, f"vlan{vid}", "virtual", description=v.name))
            d.ips.append(IP(net(n, vid, 2 + i), a, f"vlan{vid}"))
            d.ips.append(IP(net(n, vid, 1), a, f"vlan{vid}", role="vip", description="Gateway"))

    # ---- access switches: uplinks (MC-LAG to agg pair), mgmt, edge ports
    profiles = pp["profiles"]
    port_map = []
    for entry in pp["access_port_map"]:
        prof = profiles[entry["profile"]]
        for p in expand_ports(entry["ports"]):
            port_map.append((p, prof))
    used = [p for p, _ in port_map]
    if len(used) != len(set(used)):
        raise DesignError("port-profiles.yml: a port is listed twice in access_port_map")

    def resolve(ref, floor):
        if ref.get("parking"):
            return park["vid"]
        if "floor" in ref:
            return floor_vid[(floor, ref["floor"])]
        vid = int(ref["site"])
        if vid not in d.vlans:
            raise DesignError(f"port profile references unknown site VLAN {vid}")
        return vid

    mgmt_vid = infra["access_mgmt_vlan"]
    first_down = int(ports["agg_downlink_first"])
    used_agg_ports = set()
    for dev in access:
        trunk = sorted([floor_vid[(dev.floor, fn["name"])] for fn in vp["floor_functions"]]
                       + access_site_vids)
        # uplink LAG on the access switch
        d.add_iface(Interface(dev.name, "lag1", "lag", description="Uplink to AGG VSX",
                              mode="tagged", tagged=tuple(trunk), mtu=mtu))
        agg_port = f"1/1/{first_down + dev.index - 1}"
        used_agg_ports.add(agg_port)
        for side, up in enumerate(ports["access_uplinks"]):
            check_port(dev.name, up)
            check_port(agg[side], agg_port)
            d.add_iface(Interface(dev.name, up, description=f"To {agg[side]} {agg_port}", lag="lag1"))
            d.cables.append(Cable((dev.name, up), (agg[side], agg_port), "mmf-om4", "access-uplink"))
        # matching MC-LAG on BOTH agg switches (same LAG id = access index)
        for side, a in enumerate(agg):
            lag = f"lag{dev.index}"
            d.add_iface(Interface(a, lag, "lag", description=f"MC-LAG to {dev.name}",
                                  mode="tagged", tagged=tuple(trunk), mtu=mtu))
            d.add_iface(Interface(a, agg_port, description=f"To {dev.name} "
                                  f"{ports['access_uplinks'][side]}", lag=lag))
        # in-band management
        addr = net(n, mgmt_vid, int(infra["access_mgmt_first_host"]) + dev.index - 1)
        d.add_iface(Interface(dev.name, f"vlan{mgmt_vid}", "virtual", description="Management"))
        d.ips.append(IP(addr, dev.name, f"vlan{mgmt_vid}", dns_name=f"{dev.name.lower()}.{domain}"))
        dev.primary = (f"vlan{mgmt_vid}", addr)
        # edge ports
        for p, prof in sorted(port_map, key=lambda x: x[0]):
            port = check_port(dev.name, f"1/1/{p}")
            d.add_iface(Interface(
                dev.name, port, description=prof["description"],
                enabled=prof.get("enabled", True), mode=prof["mode"],
                untagged=resolve(prof["untagged"], dev.floor) if prof.get("untagged") else None,
                tagged=tuple(sorted(resolve(t, dev.floor) for t in prof.get("tagged", []))),
            ))

    if len(used_agg_ports) and max(int(p.split("/")[-1]) for p in used_agg_ports) >= \
            int(ports["vsx_keepalive"].split("/")[-1]):
        raise DesignError("too many access switches: agg downlinks collide with the keepalive port")

    # ---- every remaining physical port on core/agg/access is shut down (best practice)
    for dev in d.devices.values():
        if dev.role == "firewall":
            continue
        for port in dt_ports[dev.model]:
            if port == "mgmt" or (dev.name, port) in d.interfaces:
                continue
            if re.fullmatch(r"\d+/\d+/\d+", port):
                d.add_iface(Interface(dev.name, port, description="UNUSED", enabled=False))

    return d


# --------------------------------------------------------------------------- summary

def summary(d):
    by_role = {}
    for dev in d.devices.values():
        by_role[dev.role] = by_role.get(dev.role, 0) + 1
    return {
        "devices": len(d.devices), "by_role": by_role, "vlans": len(d.vlans),
        "prefixes": len(d.prefixes), "interfaces": len(d.interfaces),
        "lags": sum(1 for i in d.interfaces.values() if i.kind == "lag"),
        "cables": len(d.cables), "ips": len(d.ips),
    }


# ===========================================================================
# Validation rules (from nblab/checks.py)
# ===========================================================================
from collections import Counter



class Result:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, rule, msg):
        self.errors.append((rule, msg))

    def warn(self, rule, msg):
        self.warnings.append((rule, msg))


def evaluate(s):
    """Run every rule against a snapshot and return a Result.

    `s` is any object with the Snapshot interface: nblab.snapshot (REST API)
    or the ORM snapshot used inside NetBox's Custom Script.
    """
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
            lags_a = {i.name for i in s.by_device[d] if s.kind(i) == "lag" and i.name != "lag256"}
            lags_b = {i.name for i in s.by_device[peer] if s.kind(i) == "lag" and i.name != "lag256"}
            for lag in sorted(lags_a ^ lags_b):
                r.error("mclag", f"{d}/{peer}: {lag} exists on only one VSX member")
            for lag in sorted(lags_a & lags_b):
                if s.l2(d, lag) != s.l2(peer, lag):
                    r.error("mclag", f"{d}/{peer}: {lag} has different VLANs on the two members")
                if s.members(d, lag) != s.members(peer, lag):
                    r.warn("mclag", f"{d}/{peer}: {lag} uses different member ports")

    # R6 every VLAN on an access uplink has a gateway on the switches that uplink goes to
    #    (works for any number of aggregation pairs: each pair is checked on its own)
    served = {}                                   # (frozenset of upstream switches) -> VLANs
    for d in access:
        if (d, "lag1") not in s.ifaces:
            continue
        mode, untagged, tagged = s.l2(d, "lag1")
        ups = frozenset(p[0] for m in s.members(d, "lag1") if (p := s.peer(d, m)))
        if ups:
            served.setdefault(ups, set()).update(set(tagged) | ({untagged} if untagged else set()))
    for ups, vids in sorted(served.items(), key=lambda x: sorted(x[0])):
        for vid in sorted(vids):
            vips = set()
            for a in sorted(ups):
                if (a, f"vlan{vid}") not in s.ifaces:
                    r.error("gateway", f"VLAN {vid} is trunked to access but its upstream {a} "
                                       f"has no interface vlan{vid}")
                    continue
                if not s.addresses(a, f"vlan{vid}"):
                    r.error("gateway", f"{a} vlan{vid} has no IP address")
                vips |= {str(ip.address) for ip in s.addresses(a, f"vlan{vid}", vip=True)}
            if len(vips) != 1:
                r.error("gateway", f"VLAN {vid} on {'/'.join(sorted(ups))}: expected exactly one "
                                   f"VIP (active-gateway), found {sorted(vips) or 'none'}")

    # R6b a VLAN used on an access port must be carried on that switch's uplink,
    #     otherwise the users on that port are cut off from their gateway
    for d in access:
        if (d, "lag1") not in s.ifaces:
            continue
        mode, untagged, tagged = s.l2(d, "lag1")
        carried = set(tagged) | ({untagged} if untagged else set())
        missing = {}
        for i in s.by_device[d]:
            if s.kind(i) == "lag" or i.lag is not None or not i.enabled or not s.mode(i):
                continue
            _, u, tg = s.l2(d, i.name)
            for vid in (set(tg) | ({u} if u else set())) - carried:
                if s.vlans.get(vid) is not None and s.vlans[vid].name == "Parking":
                    continue
                missing.setdefault(vid, []).append(i.name)
        for vid, ports in sorted(missing.items()):
            r.error("uplink", f"{d}: VLAN {vid} is used on {len(ports)} port(s) "
                              f"({ports[0]}{', ...' if len(ports) > 1 else ''}) but not carried on lag1")

    # R6c a trunk without any VLAN is almost always a mistake
    for (d, name), i in s.ifaces.items():
        if s.is_switch(d) and s.mode(i) == "tagged" and i.lag is None:
            _, u, tg = s.l2(d, name)
            if not tg and not u:
                r.warn("trunk", f"{d} {name} is a trunk (tagged) with no VLANs")

    # R7 every VLAN used on an interface belongs to this site's VLAN group
    for (d, name), i in s.ifaces.items():
        for v in ([i.untagged_vlan] if i.untagged_vlan else []) + s.tagged(i):
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
            if s.ip_role(ip) != "vip":
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

    return r


def _fmt(l2):
    mode, untagged, tagged = l2
    if mode is None:
        return "routed/none"
    return f"{mode} untagged={untagged} tagged={_ranges(tagged)}"


def _ranges(vids):
    return vlan_ranges(vids) or "-"


# ===========================================================================
# Default design rules (from intent/*.yml and devicetypes/*.yaml)
# ===========================================================================
DEFAULT_YAML = {
    'site': '# ---------------------------------------------------------------------------\n# DESIGN INTENT for one site. This file (plus vlan-plan.yml, port-profiles.yml\n# and standards.yml) is the ONLY thing you edit by hand. Everything else\n# (devices, cables, LAGs, VLANs, IPs in NetBox, then the switch configs)\n# is derived from it by code. All data is fictional.\n# ---------------------------------------------------------------------------\nsite:\n  name: LAB01\n  slug: lab01\n  number: 1                 # site supernet = 10.<number>.0.0/16\n  description: Lab campus, 6 floors (fictional)\n  domain: lab01.lab.local\n  timezone: Europe/Berlin\n\n# Topology: 2 core (VSX) -> 2 aggregation (VSX) -> access switches (dual-homed\n# with MC-LAG). Firewalls are an HA pair, connected to the cores with MC-LAG.\nmodels:\n  core: hpe-aruba-8360-48y6c-front-to-back\n  agg: hpe-aruba-8360-48y6c-front-to-back\n  access: hpe-aruba-6300m-48g-poe4-4sfp56\n  firewall: lab-firewall-4p\n\n# How many access switches per floor. 44 access + 2 core + 2 agg + 2 FW = 50.\nfloors:\n  - {floor: 1, access_switches: 7}\n  - {floor: 2, access_switches: 7}\n  - {floor: 3, access_switches: 7}\n  - {floor: 4, access_switches: 7}\n  - {floor: 5, access_switches: 8}\n  - {floor: 6, access_switches: 8}\n\n# VSX pairs: the system MAC must be unique per pair in the whole network.\nvsx:\n  core: {system_mac: "02:00:01:00:00:01"}\n  agg:  {system_mac: "02:00:01:00:00:02"}\n\n# Physical port plan (used to build cables, LAGs and ISLs).\nports:\n  access_uplinks: ["1/1/49", "1/1/50"]    # 49 -> AGG-01, 50 -> AGG-02\n  agg_downlink_first: 1                   # access #1 -> 1/1/1, #2 -> 1/1/2 ...\n  agg_uplinks: ["1/1/49", "1/1/50"]       # 49 -> CORE-01, 50 -> CORE-02\n  core_downlinks: ["1/1/49", "1/1/50"]    # 49 -> AGG-01, 50 -> AGG-02\n  core_firewall: ["1/1/1", "1/1/2"]       # 1/1/1 -> FW-01, 1/1/2 -> FW-02\n  firewall_ports: ["port1", "port2"]      # port1 -> CORE-01, port2 -> CORE-02\n  vsx_isl: ["1/1/53", "1/1/54"]           # on core and agg pairs\n  vsx_keepalive: "1/1/48"                 # dedicated routed link, VRF KA\n',
    'vlans': '# ---------------------------------------------------------------------------\n# VLAN and IP plan. Rule: the VLAN ID is ALSO the 3rd octet of the subnet,\n# so VLAN 113 on site 1 is always 10.1.113.0/24. Easy to read, hard to mix up.\n#\n# Every /24 uses the same gateway rule (VSX active-gateway on the agg pair):\n#   .1 = virtual gateway (VIP), .2 = AGG-01, .3 = AGG-02\n# ---------------------------------------------------------------------------\n\n# Repeated on every floor:  vid = 100 + 10 * floor + offset\n# floor 1 -> 110..117, floor 6 -> 160..167  (8 functions x 6 floors = 48 VLANs)\nfloor_functions:\n  - {offset: 0, name: Users,       role: users}\n  - {offset: 1, name: Voice,       role: voice}\n  - {offset: 2, name: Printers,    role: printers}\n  - {offset: 3, name: IoT,         role: iot}\n  - {offset: 4, name: Cameras,     role: cameras}\n  - {offset: 5, name: BMS,         role: bms}\n  - {offset: 6, name: Lab,         role: lab}\n  - {offset: 7, name: Wired-Guest, role: guest}\n\n# Site-wide VLANs. on_access: true -> trunked to every access switch.\nsite_vlans:\n  - {vid: 20, name: WiFi-Corp,     role: wifi,     on_access: true}\n  - {vid: 21, name: WiFi-Guest,    role: guest,    on_access: true}\n  - {vid: 22, name: WiFi-IoT,      role: iot,      on_access: true}\n  - {vid: 23, name: WiFi-Voice,    role: voice,    on_access: true}\n  - {vid: 30, name: Servers,       role: servers,  on_access: false}\n  - {vid: 31, name: Storage,       role: servers,  on_access: false}\n  - {vid: 32, name: Backup,        role: servers,  on_access: false}\n  - {vid: 40, name: VideoConf,     role: av,       on_access: true}\n  - {vid: 50, name: AccessControl, role: security, on_access: true}\n  - {vid: 51, name: BMS-Core,      role: bms,      on_access: false}\n  - {vid: 98, name: AP-Mgmt,       role: mgmt,     on_access: true}\n  - {vid: 99, name: Net-Mgmt,      role: mgmt,     on_access: true}   # in-band mgmt of access switches\n\n# Special VLANs (not built from the rules above)\nparking_vlan: {vid: 999, name: Parking}     # unused ports; no gateway, no prefix, goes nowhere\nfirewall_transit:\n  vid: 900\n  name: FW-Transit\n  third_octet: 250        # 10.<site>.250.0/29: .1 firewall, .2 CORE-01, .3 CORE-02, .4 core VIP\n\n# Infrastructure ranges (3rd octet inside 10.<site>.0.0/16)\ninfra:\n  p2p: 251          # /31 routed links core <-> agg\n  keepalive: 252    # /31 VSX keepalive links (VRF KA)\n  oob: 253          # out-of-band mgmt ports of core/agg/firewall (VRF mgmt), gateway .1\n  loopback: 254     # /32 router IDs\n  access_mgmt_vlan: 99\n  access_mgmt_first_host: 101   # access switch #1 -> 10.<site>.99.101\n',
    'profiles': '# ---------------------------------------------------------------------------\n# Edge port profiles for access switches. VLANs are referenced by:\n#   floor: <function name>   -> that function\'s VLAN on the switch\'s own floor\n#   site:  <vid>             -> a site-wide VLAN\n#\n# Real deployments usually add 802.1X / MAC auth (port-access) on top;\n# here every port gets a static profile, which is the baseline you start from.\n# ---------------------------------------------------------------------------\nprofiles:\n  user:\n    description: USER\n    mode: tagged                 # PC untagged + IP phone tagged\n    untagged: {floor: Users}\n    tagged: [{floor: Voice}]\n    edge: true\n  ap:\n    description: AP\n    mode: tagged                 # AP management untagged, SSIDs tagged (bridged APs)\n    untagged: {site: 98}\n    tagged: [{site: 20}, {site: 21}, {site: 22}, {site: 23}]\n    edge: true\n  printer:\n    description: PRINTER\n    mode: access\n    untagged: {floor: Printers}\n    edge: true\n  camera:\n    description: CAMERA\n    mode: access\n    untagged: {floor: Cameras}\n    edge: true\n  iot:\n    description: IOT\n    mode: access\n    untagged: {floor: IoT}\n    edge: true\n  unused:\n    description: UNUSED\n    mode: access\n    untagged: {parking: true}\n    enabled: false               # shut down until somebody needs it\n    edge: true\n\n# Which ports of every access switch get which profile (ports 1-48).\naccess_port_map:\n  - {ports: "1-36",  profile: user}\n  - {ports: "37-40", profile: ap}\n  - {ports: "41-42", profile: printer}\n  - {ports: "43-44", profile: camera}\n  - {ports: "45-46", profile: iot}\n  - {ports: "47-48", profile: unused}\n',
    'standards': '# ---------------------------------------------------------------------------\n# Company-wide standards. Pushed to NetBox as a Config Context, so every\n# device sees them in NetBox under "Config Context" and the templates use them.\n# Values are fictional placeholders. NEVER put real secrets in git: the\n# templates print a <PLACEHOLDER> and the real secret is added at deploy time.\n# ---------------------------------------------------------------------------\ndns_servers: [10.1.30.53, 10.1.30.54]\nntp_servers: [10.1.30.123, 10.1.30.124]\ndhcp_servers: [10.1.30.67, 10.1.30.68]    # DHCP relay target on client VLAN gateways\nsyslog_servers: [10.1.30.60]\nsnmp:\n  v3_user: netmon\n  location_prefix: "LAB01"\ntacacs_servers: [10.1.30.49, 10.1.30.50]\nbanner: |\n  ********************************************************\n  *  Authorized access only. All activity is logged.     *\n  ********************************************************\nospf:\n  process: 1\n  area: 0.0.0.0\nspanning_tree:\n  mode: mstp\n  config_name: LAB01\n  config_revision: 1\nactive_gateway_mac: "12:00:00:00:01:00"\nmtu_infra: 9198         # jumbo on core/agg/ISL links\n',
}

DEFAULT_DEVICE_TYPES = {
    'hpe-aruba-6300m-48g-poe4-4sfp56': "---\nmanufacturer: HPE\nmodel: Aruba 6300M-48G-POE4-4SFP56\nslug: hpe-aruba-6300m-48g-poe4-4sfp56\npart_number: JL661A\nu_height: 1\nis_full_depth: true\nfront_image: true\nrear_image: true\ncomments: '[Aruba 6300 Switch Series](https://www.arubanetworks.com/assets/ds/DS_6300Series.pdf)'\nconsole-ports:\n  - name: usb-c\n    type: usb-c\n  - name: usb-ble\n    type: usb-a\nmodule-bays:\n  - name: PS1\n    position: '1'\n  - name: PS2\n    position: '2'\n  - name: FAN1\n    position: '1'\n  - name: FAN2\n    position: '2'\ninterfaces:\n  - name: 1/1/1\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/2\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/3\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/4\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/5\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/6\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/7\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/8\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/9\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/10\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/11\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/12\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/13\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/14\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/15\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/16\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/17\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/18\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/19\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/20\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/21\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/22\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/23\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/24\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/25\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/26\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/27\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/28\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/29\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/30\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/31\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/32\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/33\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/34\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/35\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/36\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/37\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/38\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/39\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/40\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/41\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/42\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/43\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/44\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/45\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/46\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/47\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/48\n    type: 1000base-t\n    poe_mode: pse\n    poe_type: type2-ieee802.3at\n  - name: 1/1/49\n    type: 50gbase-x-sfp56\n  - name: 1/1/50\n    type: 50gbase-x-sfp56\n  - name: 1/1/51\n    type: 50gbase-x-sfp56\n  - name: 1/1/52\n    type: 50gbase-x-sfp56\n  - name: mgmt\n    type: 1000base-t\n    mgmt_only: true\n",
    'hpe-aruba-8360-48y6c-front-to-back': '---\nmanufacturer: HPE\nmodel: Aruba 8360-48Y6C Front-to-Back\nslug: hpe-aruba-8360-48y6c-front-to-back\npart_number: JL704C\nu_height: 1\nis_full_depth: false\nfront_image: true\nrear_image: true\nairflow: front-to-rear\nconsole-ports:\n  - name: rj45-console\n    type: rj-45\n  - name: usb-console\n    type: usb-micro-b\n  - name: usb-ble\n    type: usb-a\npower-ports:\n  - name: ps1\n    type: iec-60320-c14\n    maximum_draw: 725\n    allocated_draw: 230\n  - name: ps2\n    type: iec-60320-c14\n    maximum_draw: 725\n    allocated_draw: 230\ninterfaces:\n  - name: 1/1/1\n    type: 25gbase-x-sfp28\n  - name: 1/1/2\n    type: 25gbase-x-sfp28\n  - name: 1/1/3\n    type: 25gbase-x-sfp28\n  - name: 1/1/4\n    type: 25gbase-x-sfp28\n  - name: 1/1/5\n    type: 25gbase-x-sfp28\n  - name: 1/1/6\n    type: 25gbase-x-sfp28\n  - name: 1/1/7\n    type: 25gbase-x-sfp28\n  - name: 1/1/8\n    type: 25gbase-x-sfp28\n  - name: 1/1/9\n    type: 25gbase-x-sfp28\n  - name: 1/1/10\n    type: 25gbase-x-sfp28\n  - name: 1/1/11\n    type: 25gbase-x-sfp28\n  - name: 1/1/12\n    type: 25gbase-x-sfp28\n  - name: 1/1/13\n    type: 25gbase-x-sfp28\n  - name: 1/1/14\n    type: 25gbase-x-sfp28\n  - name: 1/1/15\n    type: 25gbase-x-sfp28\n  - name: 1/1/16\n    type: 25gbase-x-sfp28\n  - name: 1/1/17\n    type: 25gbase-x-sfp28\n  - name: 1/1/18\n    type: 25gbase-x-sfp28\n  - name: 1/1/19\n    type: 25gbase-x-sfp28\n  - name: 1/1/20\n    type: 25gbase-x-sfp28\n  - name: 1/1/21\n    type: 25gbase-x-sfp28\n  - name: 1/1/22\n    type: 25gbase-x-sfp28\n  - name: 1/1/23\n    type: 25gbase-x-sfp28\n  - name: 1/1/24\n    type: 25gbase-x-sfp28\n  - name: 1/1/25\n    type: 25gbase-x-sfp28\n  - name: 1/1/26\n    type: 25gbase-x-sfp28\n  - name: 1/1/27\n    type: 25gbase-x-sfp28\n  - name: 1/1/28\n    type: 25gbase-x-sfp28\n  - name: 1/1/29\n    type: 25gbase-x-sfp28\n  - name: 1/1/30\n    type: 25gbase-x-sfp28\n  - name: 1/1/31\n    type: 25gbase-x-sfp28\n  - name: 1/1/32\n    type: 25gbase-x-sfp28\n  - name: 1/1/33\n    type: 25gbase-x-sfp28\n  - name: 1/1/34\n    type: 25gbase-x-sfp28\n  - name: 1/1/35\n    type: 25gbase-x-sfp28\n  - name: 1/1/36\n    type: 25gbase-x-sfp28\n  - name: 1/1/37\n    type: 25gbase-x-sfp28\n  - name: 1/1/38\n    type: 25gbase-x-sfp28\n  - name: 1/1/39\n    type: 25gbase-x-sfp28\n  - name: 1/1/40\n    type: 25gbase-x-sfp28\n  - name: 1/1/41\n    type: 25gbase-x-sfp28\n  - name: 1/1/42\n    type: 25gbase-x-sfp28\n  - name: 1/1/43\n    type: 25gbase-x-sfp28\n  - name: 1/1/44\n    type: 25gbase-x-sfp28\n  - name: 1/1/45\n    type: 25gbase-x-sfp28\n  - name: 1/1/46\n    type: 25gbase-x-sfp28\n  - name: 1/1/47\n    type: 25gbase-x-sfp28\n  - name: 1/1/48\n    type: 25gbase-x-sfp28\n  - name: 1/1/49\n    type: 100gbase-x-qsfp28\n  - name: 1/1/50\n    type: 100gbase-x-qsfp28\n  - name: 1/1/51\n    type: 100gbase-x-qsfp28\n  - name: 1/1/52\n    type: 100gbase-x-qsfp28\n  - name: 1/1/53\n    type: 100gbase-x-qsfp28\n  - name: 1/1/54\n    type: 100gbase-x-qsfp28\n  - name: mgmt\n    type: 1000base-t\n    mgmt_only: true\n',
    'lab-firewall-4p': '---\n# Fictional firewall model for the lab (no config is generated for it).\nmanufacturer: Generic\nmodel: Lab Firewall 4P\nslug: lab-firewall-4p\nu_height: 1\nis_full_depth: false\ninterfaces:\n  - name: port1\n    type: 10gbase-x-sfpp\n  - name: port2\n    type: 10gbase-x-sfpp\n  - name: port3\n    type: 10gbase-x-sfpp\n  - name: port4\n    type: 10gbase-x-sfpp\n  - name: mgmt\n    type: 1000base-t\n    mgmt_only: true\n',
}


# ===========================================================================
# NetBox database layer (from netbox/scripts/src/orm.py)
# ===========================================================================
# ===========================================================================
# ORM side: runs INSIDE NetBox (Custom Script). Uses Django models directly.
# The design (what should exist) comes from build() above; this part makes the
# database match it and reads it back for the validation rules.
# ===========================================================================
from collections import Counter as _Counter
from collections import defaultdict as _defaultdict

import yaml as _yaml
from django.contrib.contenttypes.models import ContentType

# Django models that share a name with the design dataclasses get an "Nb" prefix
from dcim.models import Cable as NbCable
from dcim.models import ConsolePortTemplate, DeviceRole, DeviceType, InterfaceTemplate, Location
from dcim.models import Device as NbDevice
from dcim.models import Interface as NbInterface
from dcim.models import Manufacturer, Platform, Site
from extras.models import ConfigContext, ConfigTemplate, Tag
from ipam.models import VLAN, VRF, IPAddress, Role, VLANGroup
from ipam.models import Prefix as NbPrefix

TAG_SLUG = "lab-managed"
PLATFORM = {"name": "Aruba AOS-CX", "slug": "aruba-aoscx"}
CONFIG_TEMPLATE_NAME = "Aruba CX"
ROLE_COLORS = {"core": "1565c0", "agg": "6a1b9a", "access": "2e7d32", "firewall": "c62828"}


def _same(a, b):
    """Equality that also treats e.g. a ZoneInfo and its name as equal."""
    return a == b or (a is not None and b is not None and str(a) == str(b))


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


class OrmApplier:
    """Create / update NetBox objects so that they match a Design."""

    def __init__(self, script, design):
        self.s = script
        self.d = design
        self.stats = _defaultdict(_Counter)
        self.errors = 0

    # ---------------------------------------------------------------- helpers
    def note(self, action, kind, obj=None, label="", loud=False):
        self.stats[kind][action] += 1
        if action != "unchanged" and loud:
            self.s.log_info(f"{action} {kind}: {label or obj}", obj=obj)

    def fail(self, msg, obj=None):
        self.errors += 1
        self.s.log_failure(msg, obj=obj)

    def ensure(self, kind, model, lookup, fields=None, m2m=None, loud=False, label=""):
        """Get the object matching `lookup`, create it or update `fields`. Returns it."""
        fields = fields or {}
        obj = model.objects.filter(**lookup).first()
        if obj is None:
            obj = model(**{**fields, **lookup})
            obj.full_clean()
            obj.save()
            action = "create"
        else:
            changed = [k for k, v in fields.items() if not _same(getattr(obj, k), v)]
            for k in changed:
                setattr(obj, k, fields[k])
            if changed:
                obj.full_clean()
                obj.save()
            action = "update" if changed else "unchanged"
        for name, values in (m2m or {}).items():
            current = set(getattr(obj, name).all())
            if current != set(values):
                getattr(obj, name).set(values)
                action = "update" if action == "unchanged" else action
        if hasattr(obj, "tags") and not obj.tags.filter(pk=self.tag.pk).exists():
            obj.tags.add(self.tag)
        self.note(action, kind, obj, label, loud)
        return obj

    # ---------------------------------------------------------------- run
    def run(self, device_types):
        self.tag, _ = Tag.objects.get_or_create(
            slug=TAG_SLUG, defaults={"name": TAG_SLUG, "color": "9e9e9e",
                                     "description": "Managed by netbox-lab. Do not edit by hand."})
        self.catalog(device_types)
        self.site_objects()
        self.vlans_prefixes()
        self.devices()
        self.interfaces()
        self.ip_addresses()
        self.cables()
        self.primary_ips()

    def catalog(self, device_types):
        d = self.d
        self.vrf = {n: self.ensure("vrf", VRF, {"name": n}, {"enforce_unique": True}) for n in d.vrfs}
        roles = sorted({p.role for p in d.prefixes} | {v.role for v in d.vlans.values() if v.role})
        self.ipam_role = {r: self.ensure("ipam-role", Role, {"slug": r}, {"name": r.capitalize()})
                          for r in roles}
        self.dev_role = {r: self.ensure("device-role", DeviceRole, {"slug": r},
                                        {"name": r.capitalize(), "color": c})
                         for r, c in ROLE_COLORS.items()}
        template = ConfigTemplate.objects.filter(name=CONFIG_TEMPLATE_NAME).first()
        if template is None:
            self.s.log_warning(f"Config Template '{CONFIG_TEMPLATE_NAME}' not found: sync it from "
                               "the data source, then run this script again to attach it.")
        self.platform = self.ensure("platform", Platform, {"slug": PLATFORM["slug"]},
                                    {"name": PLATFORM["name"], "config_template": template})
        self.dtype = device_types

    def site_objects(self):
        d = self.d
        s = d.site
        self.site = self.ensure("site", Site, {"slug": s["slug"]},
                                {"name": s["name"], "status": "active",
                                 "description": s.get("description", ""),
                                 "time_zone": s.get("timezone") or None}, loud=True)
        self.location = {}
        for slug, name, desc in d.locations:
            self.location[slug] = self.ensure("location", Location, {"slug": slug, "site": self.site},
                                              {"name": name, "description": desc, "status": "active"})
        self.ensure("config-context", ConfigContext, {"name": f"standards-{s['slug']}"},
                    {"weight": 1000, "data": d.standards,
                     "description": "Company standards for this site (netbox-lab)"},
                    m2m={"sites": [self.site]}, loud=True)

    def vlans_prefixes(self):
        d = self.d
        site_ct = ContentType.objects.get_for_model(Site)
        s = d.site
        self.group = self.ensure("vlan-group", VLANGroup, {"slug": s["slug"]},
                                 {"name": f"{s['name']} VLANs", "scope_type": site_ct,
                                  "scope_id": self.site.pk}, loud=True)
        self.vlan = {}
        for vid, v in sorted(d.vlans.items()):
            self.vlan[vid] = self.ensure(
                "vlan", VLAN, {"group": self.group, "vid": vid},
                {"name": v.name, "status": "active", "description": v.description,
                 "role": self.ipam_role.get(v.role) if v.role else None})
        for p in d.prefixes:
            self.ensure("prefix", NbPrefix, {"prefix": p.prefix, "vrf": self.vrf.get(p.vrf)},
                        {"status": p.status, "description": p.description,
                         "role": self.ipam_role.get(p.role),
                         "vlan": self.vlan.get(p.vid) if p.vid else None,
                         "scope_type": site_ct, "scope_id": self.site.pk})

    def devices(self):
        self.device = {}
        for name, dev in self.d.devices.items():
            fields = {"device_type": self.dtype[dev.model], "role": self.dev_role[dev.role],
                      "location": self.location[dev.location],
                      "status": "active", "local_context_data": dev.context or None,
                      "platform": self.platform if dev.role != "firewall" else None}
            existing = NbDevice.objects.filter(name=name, site=self.site).first()
            if existing is not None and existing.device_type != fields["device_type"]:
                self.fail(f"{name} exists with another device type; delete it first", obj=existing)
                continue
            self.device[name] = self.ensure("device", NbDevice, {"name": name, "site": self.site},
                                            fields)

    def interfaces(self):
        by_device = _defaultdict(list)
        for (dev, _), i in self.d.interfaces.items():
            by_device[dev].append(i)
        self.iface = {}
        for dev_name, wanted in by_device.items():
            dev = self.device.get(dev_name)
            if dev is None:
                continue
            have = {i.name: i for i in NbInterface.objects.filter(device=dev)}
            for i in wanted:                       # LAG / virtual first (members point to them)
                if i.name in have:
                    continue
                if i.kind == "physical":
                    self.fail(f"{dev_name}: port {i.name} does not exist on {dev.device_type}", obj=dev)
                    continue
                rec = NbInterface(device=dev, name=i.name, type="lag" if i.kind == "lag" else "virtual")
                rec.save()
                rec.tags.add(self.tag)
                have[i.name] = rec
                self.note("create", "interface", rec)
            for name, rec in have.items():
                self.iface[(dev_name, name)] = rec
            for i in wanted:
                rec = have.get(i.name)
                if rec is None:
                    continue
                fields = {"enabled": i.enabled, "description": i.description, "mode": i.mode or None,
                          "untagged_vlan": self.vlan.get(i.untagged) if i.untagged else None,
                          "lag": have.get(i.lag) if i.lag else None,
                          "vrf": self.vrf.get(i.vrf) if i.vrf else None,
                          "mtu": i.mtu, "mgmt_only": i.mgmt_only}
                changed = [k for k, v in fields.items() if not _same(getattr(rec, k), v)]
                for k in changed:
                    setattr(rec, k, fields[k])
                if changed:
                    rec.save()
                want_tagged = {self.vlan[v] for v in i.tagged}
                if set(rec.tagged_vlans.all()) != want_tagged:
                    rec.tagged_vlans.set(want_tagged)
                    changed = changed or ["tagged_vlans"]
                if not rec.tags.filter(pk=self.tag.pk).exists():
                    rec.tags.add(self.tag)
                self.note("update" if changed else "unchanged", "interface", rec)

    def ip_addresses(self):
        iface_ct = ContentType.objects.get_for_model(NbInterface)
        self.ip = {}
        for ip in self.d.ips:
            iface = self.iface.get((ip.device, ip.interface))
            if iface is None:
                continue
            self.ip[(ip.address, ip.vrf, ip.device, ip.interface)] = self.ensure(
                "ip-address", IPAddress,
                {"address": ip.address, "vrf": self.vrf.get(ip.vrf),
                 "assigned_object_type": iface_ct, "assigned_object_id": iface.pk},
                {"status": "active", "role": ip.role or None, "dns_name": ip.dns_name,
                 "description": ip.description})

    def cables(self):
        for cab in self.d.cables:
            a, b = self.iface.get(cab.a), self.iface.get(cab.b)
            label = f"{cab.a[0]} {cab.a[1]} <-> {cab.b[0]} {cab.b[1]}"
            if a is None or b is None:
                continue
            a.refresh_from_db()
            b.refresh_from_db()
            if a.cable_id and a.cable_id == b.cable_id:
                c = a.cable
                if c.type != cab.type or c.label != cab.label:
                    c.type, c.label = cab.type, cab.label
                    c.save()
                    self.note("update", "cable", c)
                else:
                    self.note("unchanged", "cable", c)
                continue
            if a.cable_id or b.cable_id:
                self.fail(f"{label}: a port is already cabled somewhere else; fix it by hand", obj=a)
                continue
            c = NbCable(type=cab.type, label=cab.label, status="connected")
            c.a_terminations = [a]
            c.b_terminations = [b]
            c.full_clean()
            c.save()
            c.tags.add(self.tag)
            self.note("create", "cable", c)

    def primary_ips(self):
        for name, dev in self.d.devices.items():
            rec = self.device.get(name)
            if rec is None or not dev.primary:
                continue
            iface, addr = dev.primary
            vrf = self.d.interfaces[(name, iface)].vrf
            ip = self.ip.get((addr, vrf, name, iface))
            if ip is None:
                continue
            rec.refresh_from_db()
            if rec.primary_ip4_id != ip.pk:
                rec.primary_ip4 = ip
                rec.save()
                self.note("update", "primary-ip", rec)
            else:
                self.note("unchanged", "primary-ip", rec)

    def summary_table(self):
        lines = ["| object | create | update | unchanged |", "|---|---:|---:|---:|"]
        for kind, c in sorted(self.stats.items()):
            lines.append(f"| {kind} | {c['create']} | {c['update']} | {c['unchanged']} |")
        return "\n".join(lines)


def import_device_type(data, tag):
    """Create a device type with its port templates (devicetype-library format)."""
    mslug = _slugify(data["manufacturer"])
    mfr, _ = Manufacturer.objects.get_or_create(slug=mslug, defaults={"name": data["manufacturer"]})
    dt = DeviceType.objects.filter(slug=data["slug"]).first()
    if dt is not None:
        return dt, False
    fields = {k: data[k] for k in ("model", "slug", "part_number", "u_height",
                                    "is_full_depth", "airflow") if k in data}
    dt = DeviceType(manufacturer=mfr, **fields)
    dt.full_clean()
    dt.save()
    dt.tags.add(tag)
    InterfaceTemplate.objects.bulk_create([
        InterfaceTemplate(device_type=dt, name=i["name"], type=i["type"],
                          mgmt_only=i.get("mgmt_only", False),
                          poe_mode=i.get("poe_mode") or None, poe_type=i.get("poe_type") or None)
        for i in data.get("interfaces", [])])
    ConsolePortTemplate.objects.bulk_create([
        ConsolePortTemplate(device_type=dt, name=c["name"], type=c["type"])
        for c in data.get("console-ports", [])])
    return dt, True


# ---------------------------------------------------------------------------
# Read a site from the database, with the same interface as nblab/snapshot.py,
# so the SAME validation rules (evaluate) run here and on the command line.
# ---------------------------------------------------------------------------
class OrmSnapshot:
    def __init__(self, site):
        self.site = site
        self.devices = {d.name: d for d in NbDevice.objects.filter(site=site)
                        .select_related("role", "platform", "location", "primary_ip4", "device_type")}
        qs = (NbInterface.objects.filter(device__site=site)
              .select_related("device", "lag", "vrf", "untagged_vlan", "cable")
              .prefetch_related("tagged_vlans"))
        self.ifaces = {(i.device.name, i.name): i for i in qs}
        self.by_device = _defaultdict(list)
        for (dev, _), i in self.ifaces.items():
            self.by_device[dev].append(i)
        iface_ct = ContentType.objects.get_for_model(NbInterface)
        by_id = {i.pk: i for i in self.ifaces.values()}
        self.ips = _defaultdict(list)
        for ip in IPAddress.objects.filter(assigned_object_type=iface_ct,
                                           assigned_object_id__in=list(by_id)).select_related("vrf"):
            i = by_id[ip.assigned_object_id]
            self.ips[(i.device.name, i.name)].append(ip)
        # VLANs of every VLAN group scoped to this site (any group name / slug)
        site_ct = ContentType.objects.get_for_model(Site)
        groups = VLANGroup.objects.filter(scope_type=site_ct, scope_id=site.pk)
        self.vlans = {v.vid: v for v in VLAN.objects.filter(group__in=groups)}
        self.prefixes = list(NbPrefix.objects.filter(_site=site).select_related("vlan", "vrf"))
        self._ctx = {}

    def role(self, dev):
        return self.devices[dev].role.slug

    def is_switch(self, dev):
        p = self.devices[dev].platform
        return p is not None and p.slug == PLATFORM["slug"]

    def context(self, dev):
        if dev not in self._ctx:
            self._ctx[dev] = self.devices[dev].get_config_context() or {}
        return self._ctx[dev]

    @staticmethod
    def kind(i):
        return i.type

    @staticmethod
    def mode(i):
        return i.mode or None

    @staticmethod
    def tagged(i):
        return list(i.tagged_vlans.all())

    @staticmethod
    def ip_role(ip):
        return ip.role or None

    def l2(self, dev, name):
        i = self.ifaces[(dev, name)]
        if i.lag_id:
            i = self.ifaces[(dev, i.lag.name)]
        return (self.mode(i), i.untagged_vlan.vid if i.untagged_vlan else None,
                tuple(sorted(v.vid for v in self.tagged(i))))

    def mtu(self, dev, name):
        i = self.ifaces[(dev, name)]
        if i.lag_id:
            i = self.ifaces[(dev, i.lag.name)]
        return i.mtu or 1500

    def peer(self, dev, name):
        i = self.ifaces[(dev, name)]
        if not i.cable_id:
            return None
        peers = i.link_peers or []
        if not peers or not isinstance(peers[0], NbInterface):
            return None
        return (peers[0].device.name, peers[0].name)

    def members(self, dev, lag):
        return sorted(i.name for i in self.by_device[dev] if i.lag_id and i.lag.name == lag)

    def addresses(self, dev, name, role=None, vip=False):
        out = []
        for ip in self.ips.get((dev, name), []):
            r = self.ip_role(ip)
            if vip and r != "vip":
                continue
            if not vip and r == "vip":
                continue
            if role and r != role:
                continue
            out.append(ip)
        return out

    @staticmethod
    def iface_net(ip):
        return ipaddress.ip_interface(str(ip.address)).network


def report_result(script, result):
    for rule, msg in result.errors:
        script.log_failure(f"[{rule}] {msg}")
    for rule, msg in result.warnings:
        script.log_warning(f"[{rule}] {msg}")
    if not result.errors and not result.warnings:
        script.log_success("All validation rules passed.")
    return f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)"


# ===========================================================================
# Scripts (from netbox/scripts/src/scripts.py)
# ===========================================================================
# ===========================================================================
# NetBox Custom Scripts (what you see under Customization -> Scripts)
# ===========================================================================
from extras.scripts import BooleanVar, IntegerVar, ObjectVar, Script, StringVar, TextVar
from utilities.exceptions import AbortScript

_SITE_DEFAULT = _yaml.safe_load(DEFAULT_YAML["site"])
_PORTS_DEFAULT = _yaml.safe_dump({"ports": _SITE_DEFAULT["ports"]}, sort_keys=False)
_FLOORS_DEFAULT = ",".join(str(f["access_switches"]) for f in _SITE_DEFAULT["floors"])
_MODELS_DEFAULT = _SITE_DEFAULT["models"]


def _model_help(role):
    return (f"Leave empty to use {_MODELS_DEFAULT[role]} (imported automatically "
            f"from devicetype-library if it is missing).")


class BuildSite(Script):
    """Build a complete campus site from a few design decisions."""

    class Meta:
        name = "Build site"
        description = ("Create a whole site from the design rules: devices, cables, LAGs/MC-LAGs, "
                       "VSX, VLANs, prefixes and IP addresses. Untick 'Commit changes' to preview.")
        commit_default = False
        scheduling_enabled = False
        fieldsets = (
            ("Site", ("site_name", "site_number", "site_description", "domain", "timezone")),
            ("Size", ("floors",)),
            ("Hardware", ("core_agg_type", "access_type", "firewall_type",
                          "vsx_core_mac", "vsx_agg_mac")),
            ("Design rules (YAML, advanced)", ("ports_plan", "vlan_plan", "port_profiles",
                                               "standards")),
        )

    site_name = StringVar(label="Site name", default=_SITE_DEFAULT["site"]["name"],
                          regex=r"^[A-Z0-9]+$",
                          description="Upper-case letters and digits, used in every device name")
    site_number = IntegerVar(label="Site number", default=_SITE_DEFAULT["site"]["number"],
                             min_value=1, max_value=254,
                             description="Second octet of all addresses: 10.<number>.x.x")
    site_description = StringVar(label="Description", required=False,
                                 default=_SITE_DEFAULT["site"].get("description", ""))
    domain = StringVar(default=_SITE_DEFAULT["site"].get("domain", "lab.local"))
    timezone = StringVar(default=_SITE_DEFAULT["site"].get("timezone", "Europe/Berlin"))
    floors = StringVar(label="Access switches per floor", default=_FLOORS_DEFAULT,
                       regex=r"^\s*\d+(\s*,\s*\d+)*\s*$",
                       description="Comma separated, one number per floor. "
                                   "7,7,8 = 3 floors with 7, 7 and 8 access switches")
    core_agg_type = ObjectVar(model=DeviceType, required=False, label="Core / aggregation model",
                              description=_model_help("core"))
    access_type = ObjectVar(model=DeviceType, required=False, label="Access switch model",
                            description=_model_help("access"))
    firewall_type = ObjectVar(model=DeviceType, required=False, label="Firewall model",
                              description=_model_help("firewall"))
    vsx_core_mac = StringVar(label="VSX system MAC (core)",
                             default=_SITE_DEFAULT["vsx"]["core"]["system_mac"])
    vsx_agg_mac = StringVar(label="VSX system MAC (aggregation)",
                            default=_SITE_DEFAULT["vsx"]["agg"]["system_mac"])
    ports_plan = TextVar(label="Physical port plan", default=_PORTS_DEFAULT, required=True)
    vlan_plan = TextVar(label="VLAN / IP plan", default=DEFAULT_YAML["vlans"], required=True)
    port_profiles = TextVar(label="Port profiles", default=DEFAULT_YAML["profiles"], required=True)
    standards = TextVar(label="Standards (config context)", default=DEFAULT_YAML["standards"],
                        required=True)

    def run(self, data, commit):
        tag, _ = Tag.objects.get_or_create(slug=TAG_SLUG, defaults={"name": TAG_SLUG,
                                                                    "color": "9e9e9e"})
        # 1. device types: chosen in the form, or the bundled defaults
        chosen = {"core": data.get("core_agg_type"), "agg": data.get("core_agg_type"),
                  "access": data.get("access_type"), "firewall": data.get("firewall_type")}
        models, dtypes = {}, {}
        for role, dt in chosen.items():
            if dt is None:
                slug = _MODELS_DEFAULT[role]
                dt, created = import_device_type(_yaml.safe_load(DEFAULT_DEVICE_TYPES[slug]), tag)
                if created:
                    self.log_info(f"Imported device type {dt}", obj=dt)
            models[role] = dt.slug
            dtypes[dt.slug] = dt
        dt_ports = {slug: list(InterfaceTemplate.objects.filter(device_type=dt)
                               .values_list("name", flat=True)) for slug, dt in dtypes.items()}

        # 2. the intent, from the form
        try:
            floors = [int(x) for x in data["floors"].split(",")]
            intent = {
                "site": {
                    "site": {"name": data["site_name"], "slug": data["site_name"].lower(),
                             "number": data["site_number"], "description": data["site_description"] or "",
                             "domain": data["domain"], "timezone": data["timezone"]},
                    "models": models,
                    "floors": [{"floor": i + 1, "access_switches": n} for i, n in enumerate(floors)],
                    "vsx": {"core": {"system_mac": data["vsx_core_mac"]},
                            "agg": {"system_mac": data["vsx_agg_mac"]}},
                    "ports": _yaml.safe_load(data["ports_plan"])["ports"],
                },
                "vlans": _yaml.safe_load(data["vlan_plan"]),
                "profiles": _yaml.safe_load(data["port_profiles"]),
                "standards": _yaml.safe_load(data["standards"]),
            }
            design = build(intent, dt_ports)
        except (DesignError, KeyError, ValueError, TypeError, _yaml.YAMLError) as exc:
            self.log_failure(f"Design error: {exc}")
            raise AbortScript(f"Design error: {exc}")

        s = summary(design)
        self.log_info(f"Design: {s['devices']} devices {s['by_role']}, {s['vlans']} VLANs, "
                      f"{s['prefixes']} prefixes, {s['lags']} LAGs, {s['cables']} cables, "
                      f"{s['ips']} IP addresses")

        # 3. make NetBox match the design
        applier = OrmApplier(self, design)
        applier.run(dtypes)
        if applier.errors:
            raise AbortScript(f"{applier.errors} error(s), nothing was saved.")

        created = applier.stats["device"]["create"]
        if created:
            self.log_info(f"{created} new device(s), {applier.stats['cable']['create']} new cable(s), "
                          f"{applier.stats['ip-address']['create']} new IP address(es). "
                          "Full table in the Output below.")

        # 4. validate what is now in NetBox
        result = evaluate(OrmSnapshot(applier.site))
        verdict = report_result(self, result)
        if commit:
            self.log_success(f"Site {data['site_name']} saved. Validation: {verdict}. "
                             "Open any switch and use the 'Render Config' tab.", obj=applier.site)
        else:
            self.log_info("Preview only: nothing was saved. Tick 'Commit changes' to build it.")
        return applier.summary_table()


class ValidateSite(Script):
    """Run the design rules against the data that is in NetBox right now."""

    class Meta:
        name = "Validate site"
        description = ("Check a site before generating configs: dual-homed access, identical VLANs "
                       "on both ends of every link, symmetric MC-LAGs, VSX, gateways, MTU, "
                       "duplicate IPs, naming. Read-only.")
        commit_default = False
        scheduling_enabled = True

    site = ObjectVar(model=Site)

    def run(self, data, commit):
        result = evaluate(OrmSnapshot(data["site"]))
        return report_result(self, result)


class ResetSite(Script):
    """Delete a lab site (only objects tagged lab-managed). For practice."""

    class Meta:
        name = "Reset site (lab only)"
        description = "Delete everything netbox-lab created for one site. Hand-made objects stay."
        commit_default = False
        scheduling_enabled = False

    site = ObjectVar(model=Site)
    confirm = BooleanVar(label="I really want to delete this site",
                         description="Also tick 'Commit changes' below")

    def run(self, data, commit):
        site = data["site"]
        if not data["confirm"]:
            raise AbortScript("Tick the confirmation box first.")
        tagged = {"tags__slug": TAG_SLUG}
        devices = NbDevice.objects.filter(site=site, **tagged)
        iface_ct = ContentType.objects.get_for_model(NbInterface)
        ifaces = NbInterface.objects.filter(device__in=devices)
        counts = {
            "cables": NbCable.objects.filter(terminations__termination_type=iface_ct,
                                           terminations___device__in=devices, **tagged).distinct(),
            "ip addresses": IPAddress.objects.filter(assigned_object_type=iface_ct,
                                                     assigned_object_id__in=ifaces.values("pk"),
                                                     **tagged),
        }
        for label, qs in counts.items():
            n = qs.count()
            for obj in qs:
                obj.delete()
            self.log_info(f"Deleted {n} {label}")
        group = VLANGroup.objects.filter(slug=site.slug, **tagged).first()
        for label, qs in (
                ("devices", devices),
                ("prefixes", NbPrefix.objects.filter(_site=site, **tagged)),
                ("VLANs", VLAN.objects.filter(group=group, **tagged) if group else VLAN.objects.none()),
                ("VLAN groups", VLANGroup.objects.filter(slug=site.slug, **tagged)),
                ("config contexts", ConfigContext.objects.filter(name=f"standards-{site.slug}")),
                ("locations", Location.objects.filter(site=site, **tagged))):
            n = qs.count()
            for obj in qs:
                obj.delete()
            self.log_info(f"Deleted {n} {label}")
        if site.tags.filter(slug=TAG_SLUG).exists():
            site.delete()
            self.log_success(f"Deleted site {site}")
        return "done"
