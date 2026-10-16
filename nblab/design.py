"""Turn the design intent (intent/*.yml) into a complete, explicit design.

This module is pure Python: it does not talk to NetBox. It answers the question
"what EXACTLY should exist?" - every device, interface, LAG, cable, VLAN,
prefix and IP address - so that it can be tested offline and then synced.
"""

import ipaddress
import re
from dataclasses import dataclass, field

from .config import DEVICETYPE_DIR, INTENT_DIR, load_yaml


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


def load_device_type_ports():
    """slug -> list of interface names, read from the device type YAML files."""
    ports = {}
    for path in sorted(DEVICETYPE_DIR.glob("*.yaml")):
        data = load_yaml(path)
        ports[data["slug"]] = [i["name"] for i in data.get("interfaces", [])]
    return ports


def load_intent(site_file="site-lab01.yml"):
    return {
        "site": load_yaml(INTENT_DIR / site_file),
        "vlans": load_yaml(INTENT_DIR / "vlan-plan.yml"),
        "profiles": load_yaml(INTENT_DIR / "port-profiles.yml"),
        "standards": load_yaml(INTENT_DIR / "standards.yml"),
    }


def net(n, octet, host=None, plen=24):
    if host is None:
        return f"10.{n}.{octet}.0/{plen}"
    return f"10.{n}.{octet}.{host}/{plen}"


# --------------------------------------------------------------------------- build

def build(intent=None):
    intent = intent or load_intent()
    s = intent["site"]
    vp = intent["vlans"]
    pp = intent["profiles"]
    site = s["site"]
    n = int(site["number"])
    sname = site["name"]
    domain = site.get("domain", "lab.local")
    ports = s["ports"]
    dt_ports = load_device_type_ports()

    d = Design(site=site, standards=intent["standards"])

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
