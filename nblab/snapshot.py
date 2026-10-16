"""Read one site from NetBox into plain Python structures.

Used by both 'check' and 'render'. It reads NetBox, NOT the intent files:
NetBox is the source of truth, so whatever somebody changed in the GUI is
exactly what gets validated and turned into configs.
"""

import ipaddress
from collections import defaultdict

from .config import PLATFORM, load_yaml, INTENT_DIR


class Snapshot:
    def __init__(self, nb, site_slug=None):
        site_slug = site_slug or load_yaml(INTENT_DIR / "site-lab01.yml")["site"]["slug"]
        self.nb = nb
        self.site = nb.dcim.sites.get(slug=site_slug)
        if self.site is None:
            raise SystemExit(f"Site '{site_slug}' not found in NetBox. Run 'apply' first.")
        sid = self.site.id

        self.devices = {d.name: d for d in nb.dcim.devices.filter(site_id=sid, limit=0)}
        self.ifaces = {(i.device.name, i.name): i
                       for i in nb.dcim.interfaces.filter(site_id=sid, limit=0)}
        self.by_device = defaultdict(list)
        for (dev, _), i in self.ifaces.items():
            self.by_device[dev].append(i)

        self.ips = defaultdict(list)              # (device, interface) -> [ip records]
        for ip in nb.ipam.ip_addresses.filter(assigned_to_interface=True, limit=0):
            obj = ip.assigned_object
            dev = getattr(getattr(obj, "device", None), "name", None)
            if dev in self.devices:
                self.ips[(dev, obj.name)].append(ip)

        group = nb.ipam.vlan_groups.get(slug=site_slug)
        self.vlans = {v.vid: v for v in nb.ipam.vlans.filter(group_id=group.id, limit=0)} if group else {}
        self.prefixes = list(nb.ipam.prefixes.filter(site_id=sid, limit=0))

    # ------------------------------------------------------------------ helpers
    def role(self, dev):
        return self.devices[dev].role.slug

    def is_switch(self, dev):
        p = self.devices[dev].platform
        return p is not None and p.slug == PLATFORM["slug"]

    def context(self, dev):
        return self.devices[dev].config_context or {}

    def l2(self, dev, name):
        """Effective L2 settings (a LAG member uses its parent LAG's settings)."""
        i = self.ifaces[(dev, name)]
        if i.lag is not None:
            i = self.ifaces[(dev, i.lag.name)]
        mode = i.mode.value if i.mode else None
        return (mode,
                i.untagged_vlan.vid if i.untagged_vlan else None,
                tuple(sorted(v.vid for v in (i.tagged_vlans or []))))

    def mtu(self, dev, name):
        """Effective MTU (a LAG member uses its parent LAG's MTU, default 1500)."""
        i = self.ifaces[(dev, name)]
        if i.lag is not None:
            i = self.ifaces[(dev, i.lag.name)]
        return i.mtu or 1500

    def peer(self, dev, name):
        """(device, interface) at the other end of the cable, or None."""
        i = self.ifaces[(dev, name)]
        peers = i.link_peers or []
        if not peers or i.link_peers_type != "dcim.interface":
            return None
        p = peers[0]
        return (p.device.name, p.name)

    def members(self, dev, lag):
        return sorted(i.name for i in self.by_device[dev] if i.lag is not None and i.lag.name == lag)

    def addresses(self, dev, name, role=None, vip=False):
        out = []
        for ip in self.ips.get((dev, name), []):
            r = ip.role.value if ip.role else None
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
