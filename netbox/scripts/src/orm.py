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
                                            fields, loud=True)

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
        group = VLANGroup.objects.filter(slug=site.slug).first()
        self.vlans = {v.vid: v for v in VLAN.objects.filter(group=group)} if group else {}
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
