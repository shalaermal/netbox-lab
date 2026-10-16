"""Generate switch configurations from NetBox.

NetBox data -> a clean per-device "context" (dict) -> Jinja2 templates.
All decisions (which ports are edge ports, which interfaces run OSPF, where
the default route points...) are made here from NetBox data, so the templates
stay simple and only deal with syntax.
"""

import ipaddress
import re
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import CONFIG_DIR, TEMPLATE_DIR
from .snapshot import Snapshot


def vlan_ranges(vids):
    """[20,21,22,23,40] -> '20-23,40'"""
    vids = sorted(set(int(v) for v in vids))
    out, i = [], 0
    while i < len(vids):
        j = i
        while j + 1 < len(vids) and vids[j + 1] == vids[j] + 1:
            j += 1
        out.append(str(vids[i]) if i == j else f"{vids[i]}-{vids[j]}")
        i = j + 1
    return ",".join(out)


def natural_key(name):
    order = {"mgmt": 0, "lag": 1, "phy": 2, "loopback": 3, "vlan": 4}
    if name == "mgmt":
        kind = "mgmt"
    elif name.startswith("lag"):
        kind = "lag"
    elif name.startswith("loopback"):
        kind = "loopback"
    elif name.startswith("vlan"):
        kind = "vlan"
    else:
        kind = "phy"
    return (order[kind], [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)])


def cx_name(name):
    """NetBox name -> AOS-CX syntax: lag12 -> 'lag 12', vlan99 -> 'vlan 99'."""
    m = re.fullmatch(r"(lag|vlan|loopback)(\d+)", name)
    return f"{m.group(1)} {m.group(2)}" if m else name


def build_context(s, dev):
    rec = s.devices[dev]
    role = s.role(dev)
    cfg = s.context(dev)
    vsx_ctx = cfg.get("vsx")
    mgmt_vrf = "mgmt" if role in ("core", "agg") else "default"

    # VLANs that access switches use (they get DHCP relay on the gateway)
    access_vids = set()
    for d in s.devices:
        if s.is_switch(d) and s.role(d) == "access" and (d, "lag1") in s.ifaces:
            _, u, t = s.l2(d, "lag1")
            access_vids |= set(t) | ({u} if u else set())
    access_mgmt = cfg_access_mgmt_vid(s)
    mgmt_vid = None
    if role == "access":
        for i in s.by_device[dev]:
            if i.name.startswith("vlan") and s.addresses(dev, i.name):
                mgmt_vid = int(i.name[4:])

    ifaces, vlans_used = [], set()
    for i in sorted(s.by_device[dev], key=lambda x: natural_key(x.name)):
        name = i.name
        kind = ("mgmt" if name == "mgmt" else "lag" if i.type.value == "lag" else
                "svi" if name.startswith("vlan") else
                "loopback" if name.startswith("loopback") else "physical")
        mode = i.mode.value if i.mode else None
        untagged = i.untagged_vlan.vid if i.untagged_vlan else None
        tagged = sorted(v.vid for v in (i.tagged_vlans or []))
        vlans_used |= set(tagged) | ({untagged} if untagged else set())
        ips = [str(ip.address) for ip in s.addresses(dev, name)]
        vips = [str(ip.address).split("/")[0] for ip in s.addresses(dev, name, vip=True)]
        vrf = i.vrf.name if i.vrf else None
        peer = s.peer(dev, name)
        is_member = i.lag is not None
        lag_id = int(i.name[3:]) if kind == "lag" else None
        x = {
            "name": name, "cx": cx_name(name), "kind": kind,
            "description": i.description or "", "enabled": i.enabled,
            "mode": mode, "untagged": untagged, "tagged": tagged,
            "allowed": vlan_ranges(tagged + ([untagged] if untagged else [])),
            "member_of": int(i.lag.name[3:]) if is_member else None,
            "lag_id": lag_id,
            "isl": kind == "lag" and vsx_ctx is not None and lag_id == 256,
            "mclag": kind == "lag" and vsx_ctx is not None and lag_id != 256,
            "mtu": i.mtu, "vrf": vrf if vrf not in (None, "mgmt") else None,
            "ips": ips, "vip": vips[0] if vips else None,
            "routed": bool(ips) and kind == "physical",
            "edge": False, "root_guard": False, "dhcp_trust": False,
            "ospf": False, "ospf_p2p": False, "ospf_passive": False, "helpers": [],
        }
        if kind == "svi":
            vlans_used.add(int(name[4:]))
        # edge ports: access-switch ports with a VLAN mode that are not LAG members
        if role == "access" and kind == "physical" and mode and not is_member:
            x["edge"] = True
        # MC-LAG down to access switches: never accept a better root bridge from below
        if role == "agg" and x["mclag"]:
            x["root_guard"] = True
        # access uplink: the only place where DHCP server replies are trusted
        if role == "access" and kind == "lag":
            x["dhcp_trust"] = True
        # OSPF on routed links between switches and on the loopback
        if x["routed"] and vrf is None and peer and s.is_switch(peer[0]):
            x["ospf"] = x["ospf_p2p"] = True
        if kind == "loopback":
            x["ospf"] = True
        if kind == "svi" and role == "agg" and ips:
            x["ospf"] = x["ospf_passive"] = True
            vid = int(name[4:])
            if vid in access_vids and vid != access_mgmt:
                x["helpers"] = cfg.get("dhcp_servers", [])
        ifaces.append(x)

    vlans = [{"vid": v, "name": s.vlans[v].name if v in s.vlans else f"VLAN{v}"}
             for v in sorted(vlans_used)]

    # management IP / gateway
    mgmt = next((x for x in ifaces if x["kind"] == "mgmt"), None)
    mgmt_gw = None
    if mgmt and mgmt["ips"]:
        mgmt_gw = str(ipaddress.ip_interface(mgmt["ips"][0]).network.network_address + 1)

    # static routes
    routes = []
    if role == "access" and mgmt_vid:
        gw = _vip_in_network(s, ifaces, dev, f"vlan{mgmt_vid}")
        if gw:
            routes.append({"prefix": "0.0.0.0/0", "nexthop": gw, "comment": "default via AGG VIP"})
    if role == "core":
        fw_ip = _firewall_vip(s, ifaces)
        if fw_ip:
            routes.append({"prefix": "0.0.0.0/0", "nexthop": fw_ip, "comment": "default to firewall"})

    # VSX
    vsx = None
    if vsx_ctx:
        ka = next((x for x in ifaces if x["vrf"] == "KA" and x["ips"]), None)
        vsx = dict(vsx_ctx)
        if ka:
            own = ipaddress.ip_interface(ka["ips"][0])
            peer_ip = next(str(a) for a in own.network if a != own.ip)
            vsx.update({"keepalive_src": str(own.ip), "keepalive_peer": peer_ip})

    loop = next((x for x in ifaces if x["kind"] == "loopback" and x["ips"]), None)
    router_id = loop["ips"][0].split("/")[0] if loop else None

    uplink = next((x for x in ifaces if x["dhcp_trust"]), None)
    snooping = []
    if uplink:
        snooping = sorted(v for v in uplink["tagged"] + ([uplink["untagged"]] if uplink["untagged"] else [])
                          if v != mgmt_vid)

    return {
        "hostname": dev, "role": role, "site": s.site.name,
        "location": rec.location.name if rec.location else "",
        "model": rec.device_type.model, "serial": rec.serial or "",
        "primary_ip": str(rec.primary_ip4.address) if rec.primary_ip4 else None,
        "std": cfg, "mgmt_vrf": mgmt_vrf, "mgmt": mgmt, "mgmt_gw": mgmt_gw,
        "vlans": vlans, "interfaces": ifaces, "routes": routes, "vsx": vsx,
        "router_id": router_id, "ospf_enabled": router_id is not None,
        "default_originate": role == "core",
        "dhcp_snooping_vlans": snooping if role == "access" else [],
        "stp_priority": 0 if role in ("core", "agg") else None,
        "has_vrf_ka": any(x["vrf"] == "KA" for x in ifaces),
    }


def cfg_access_mgmt_vid(s):
    """VLAN of the access switches' in-band management (no DHCP relay there)."""
    for d in s.devices:
        if s.is_switch(d) and s.role(d) == "access":
            for i in s.by_device[d]:
                if i.name.startswith("vlan") and s.addresses(d, i.name):
                    return int(i.name[4:])
    return None


def _vip_in_network(s, ifaces, dev, iface):
    ip = s.addresses(dev, iface)
    if not ip:
        return None
    net = s.iface_net(ip[0])
    for (d, _), ips in s.ips.items():
        for x in ips:
            if x.role and x.role.value == "vip" and s.iface_net(x) == net:
                return str(x.address).split("/")[0]
    return None


def _firewall_vip(s, ifaces):
    for (d, _), ips in s.ips.items():
        if s.role(d) != "firewall":
            continue
        for x in ips:
            if x.role and x.role.value == "vip":
                return str(x.address).split("/")[0]
    return None


def environment():
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    env.filters["vlan_ranges"] = vlan_ranges
    return env


def render_all(nb, force=False, only=None):
    from .checks import run_checks

    s = Snapshot(nb)
    print("Running checks first ...")
    ok = run_checks(nb, snap=s)
    if not ok and not force:
        print("\nNot rendering: fix the errors above (or use --force).")
        return False

    env = environment()
    out_dir = CONFIG_DIR / s.site.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for dev in sorted(s.devices):
        if not s.is_switch(dev) or (only and dev != only):
            continue
        ctx = build_context(s, dev)
        text = env.get_template("aruba-cx/main.j2").render(**ctx)
        text = "\n".join(line for line in text.splitlines() if line.strip()) + "\n"
        (out_dir / f"{dev}.cfg").write_text(text)
        count += 1
    print(f"\nRendered {count} config(s) into {out_dir.relative_to(CONFIG_DIR.parent)}/")
    return True
