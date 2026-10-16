"""Make NetBox match the design.

    plan  : read-only, prints what WOULD be created / updated / deleted
    apply : makes the changes (idempotent: running it twice changes nothing)

Only objects carrying the tag "lab-managed" are ever deleted (--prune), so
anything created by hand in NetBox is never touched by the prune step.
"""

from collections import Counter, defaultdict

from .config import DEVICETYPE_DIR, PLATFORM, TAG, load_yaml
from .design import build

ROLE_COLORS = {"core": "1565c0", "agg": "6a1b9a", "access": "2e7d32", "firewall": "c62828"}


def _norm(value):
    """Normalise a value from pynetbox serialize() or from our desired fields."""
    if isinstance(value, list):
        out = [_norm(v) for v in value]
        try:
            return sorted(out)
        except TypeError:
            return out
    if isinstance(value, tuple):
        return _norm(list(value))
    if isinstance(value, dict) and "value" in value and "label" in value:
        return value["value"]
    return value


def diff(obj, fields):
    """Return {field: new_value} for every field whose current value differs."""
    current = obj.serialize()
    changes = {}
    for key, want in fields.items():
        have = current.get(key)
        if key == "custom_fields":
            have = have or {}
            if any(have.get(k) != v for k, v in want.items()):
                changes[key] = want
            continue
        if key == "tags":
            have_ids = sorted(t if isinstance(t, int) else t.get("id") for t in (have or []))
            if have_ids != sorted(want):
                changes[key] = want
            continue
        if _norm(have) != _norm(want):
            if (have in (None, "", [], {}) and want in (None, "", [], {})):
                continue
            changes[key] = want
    return changes


class Syncer:
    def __init__(self, nb, apply=False, prune=False, verbose=False):
        self.nb = nb
        self.apply = apply
        self.prune = prune
        self.verbose = verbose
        self.stats = defaultdict(Counter)
        self.errors = []
        self.d = build()

    # ------------------------------------------------------------------ output
    def log(self, action, kind, label, detail=""):
        self.stats[kind][action] += 1
        if action == "unchanged":
            return
        if self.verbose or kind in ("device", "site", "vlan", "prefix", "cable", "devicetype"):
            sym = {"create": "+", "update": "~", "delete": "-"}[action]
            print(f"  {sym} {action:7s} {kind:10s} {label}{('  ' + detail) if detail else ''}")

    def fail(self, msg):
        self.errors.append(msg)
        print(f"  ! ERROR {msg}")

    # ------------------------------------------------------------------ generic ensure
    def ensure(self, kind, endpoint, lookup, fields=None, label=None, create_extra=None):
        fields = dict(fields or {})
        label = label or ", ".join(f"{v}" for v in lookup.values())
        if any(v is None for v in lookup.values()):
            self.log("create", kind, label)          # parent will be created first
            return None
        obj = endpoint.get(**lookup)
        if obj is None:
            self.log("create", kind, label)
            if self.apply:
                payload = {**{k.replace("_id", "") if k.endswith("_id") else k: v
                              for k, v in lookup.items()}, **fields, **(create_extra or {})}
                try:
                    obj = endpoint.create(payload)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"create {kind} {label}: {exc}")
            return obj
        changes = diff(obj, fields)
        if changes:
            self.log("update", kind, label, ", ".join(sorted(changes)))
            if self.apply:
                try:
                    obj.update(changes)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"update {kind} {label}: {exc}")
        else:
            self.log("unchanged", kind, label)
        return obj

    @staticmethod
    def oid(obj):
        return obj.id if obj is not None else None

    # ------------------------------------------------------------------ run
    def run(self):
        nb, d = self.nb, self.d
        mode = "APPLY" if self.apply else "PLAN (read-only)"
        print(f"== {mode}: site {d.site['name']} ==")

        tag = self.ensure("tag", nb.extras.tags, {"slug": TAG},
                          {"name": TAG, "color": "9e9e9e",
                           "description": "Managed by netbox-lab code. Do not edit by hand."})
        self.tags = [tag.id] if tag else []

        self.sync_catalog()
        self.sync_site()
        self.sync_vlans_prefixes()
        self.sync_devices()
        self.sync_interfaces()
        self.sync_ips()
        self.sync_cables()
        self.sync_primary_ips()
        self.report()
        return not self.errors

    # ------------------------------------------------------------------ catalog
    def sync_catalog(self):
        nb = self.nb
        t = self.tags
        self.vrf = {name: self.ensure("vrf", nb.ipam.vrfs, {"name": name},
                                      {"enforce_unique": True, "tags": t})
                    for name in self.d.vrfs}
        roles = sorted({p.role for p in self.d.prefixes} | {v.role for v in self.d.vlans.values() if v.role})
        self.ipam_role = {r: self.ensure("ipam-role", nb.ipam.roles, {"slug": r},
                                         {"name": r.capitalize(), "tags": t}) for r in roles}
        self.dev_role = {r: self.ensure("dev-role", nb.dcim.device_roles, {"slug": r},
                                        {"name": r.capitalize(), "color": c, "tags": t})
                         for r, c in ROLE_COLORS.items()}
        self.platform = self.ensure("platform", nb.dcim.platforms, {"slug": PLATFORM["slug"]},
                                    {"name": PLATFORM["name"], "tags": t})
        self.dtype = {}
        for path in sorted(DEVICETYPE_DIR.glob("*.yaml")):
            self.dtype[load_yaml(path)["slug"]] = self.import_device_type(load_yaml(path))

    def import_device_type(self, data):
        """Create a device type with its port templates (devicetype-library format).
        Existing device types are left alone."""
        nb = self.nb
        mfr_name = data["manufacturer"]
        mfr = self.ensure("manufacturer", nb.dcim.manufacturers,
                          {"slug": mfr_name.lower().replace(" ", "-")},
                          {"name": mfr_name, "tags": self.tags})
        dt = nb.dcim.device_types.get(slug=data["slug"])
        if dt is not None:
            self.log("unchanged", "devicetype", data["slug"])
            return dt
        self.log("create", "devicetype", data["model"],
                 f"({len(data.get('interfaces', []))} interfaces)")
        if not self.apply:
            return None
        fields = {k: data[k] for k in ("model", "slug", "part_number", "u_height",
                                        "is_full_depth", "airflow") if k in data}
        dt = nb.dcim.device_types.create({**fields, "manufacturer": mfr.id, "tags": self.tags})
        ifaces = [{"device_type": dt.id, "name": i["name"], "type": i["type"],
                   "mgmt_only": i.get("mgmt_only", False),
                   **({"poe_mode": i["poe_mode"]} if "poe_mode" in i else {}),
                   **({"poe_type": i["poe_type"]} if "poe_type" in i else {})}
                  for i in data.get("interfaces", [])]
        if ifaces:
            nb.dcim.interface_templates.create(ifaces)
        consoles = [{"device_type": dt.id, "name": c["name"], "type": c["type"]}
                    for c in data.get("console-ports", [])]
        if consoles:
            nb.dcim.console_port_templates.create(consoles)
        return dt

    # ------------------------------------------------------------------ site
    def sync_site(self):
        nb, d, t = self.nb, self.d, self.tags
        s = d.site
        self.site = self.ensure("site", nb.dcim.sites, {"slug": s["slug"]},
                                {"name": s["name"], "status": "active",
                                 "description": s.get("description", ""),
                                 "time_zone": s.get("timezone"), "tags": t})
        sid = self.oid(self.site)
        self.location = {}
        for slug, name, desc in d.locations:
            self.location[slug] = self.ensure(
                "location", nb.dcim.locations, {"slug": slug},
                {"name": name, "description": desc, "status": "active", "tags": t},
                create_extra={"site": sid})
        # company standards -> config context bound to this site
        self.ensure("context", nb.extras.config_contexts, {"name": f"standards-{s['slug']}"},
                    {"weight": 1000, "data": d.standards, "sites": [sid] if sid else [],
                     "description": "Generated from intent/standards.yml"})

    # ------------------------------------------------------------------ VLANs / prefixes
    def sync_vlans_prefixes(self):
        nb, d, t = self.nb, self.d, self.tags
        s = d.site
        sid = self.oid(self.site)
        self.vlan_group = self.ensure(
            "vlan-group", nb.ipam.vlan_groups, {"slug": s["slug"]},
            {"name": f"{s['name']} VLANs", "scope_type": "dcim.site", "scope_id": sid, "tags": t})
        gid = self.oid(self.vlan_group)

        existing = {}
        if gid:
            existing = {v.vid: v for v in nb.ipam.vlans.filter(group_id=gid, limit=0)}
        self.vlan = {}
        for vid, v in sorted(d.vlans.items()):
            fields = {"name": v.name, "status": "active", "description": v.description,
                      "role": self.oid(self.ipam_role.get(v.role)) if v.role else None, "tags": t}
            obj = existing.pop(vid, None)
            self.vlan[vid] = self._ensure_known("vlan", nb.ipam.vlans, obj,
                                                {"vid": vid, "group": gid, **fields}, fields,
                                                f"{vid} {v.name}")
        for vid, obj in existing.items():
            self._orphan("vlan", obj, f"{vid} {obj.name}")

        want = {}
        for p in d.prefixes:
            want[(p.prefix, p.vrf)] = p
        have = {(str(p.prefix), p.vrf.name if p.vrf else None): p
                for p in nb.ipam.prefixes.filter(tag=TAG, limit=0)} if self.tags else {}
        for key, p in want.items():
            fields = {"status": p.status, "description": p.description,
                      "role": self.oid(self.ipam_role.get(p.role)),
                      "vlan": self.oid(self.vlan.get(p.vid)) if p.vid else None,
                      "scope_type": "dcim.site", "scope_id": sid, "tags": t}
            obj = have.pop(key, None)
            if obj is None:
                obj = nb.ipam.prefixes.get(prefix=p.prefix, vrf_id=self.oid(self.vrf.get(p.vrf))
                                           if p.vrf else "null")
            self._ensure_known("prefix", nb.ipam.prefixes, obj,
                               {"prefix": p.prefix, "vrf": self.oid(self.vrf.get(p.vrf)), **fields},
                               fields, f"{p.prefix}{' vrf ' + p.vrf if p.vrf else ''}")
        for key, obj in have.items():
            self._orphan("prefix", obj, key[0])

    def _ensure_known(self, kind, endpoint, obj, create_payload, fields, label):
        """Like ensure(), but the existing object (or None) was already looked up."""
        if obj is None:
            self.log("create", kind, label)
            if self.apply:
                try:
                    return endpoint.create(create_payload)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"create {kind} {label}: {exc}")
            return None
        changes = diff(obj, fields)
        if changes:
            self.log("update", kind, label, ", ".join(sorted(changes)))
            if self.apply:
                try:
                    obj.update(changes)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"update {kind} {label}: {exc}")
        else:
            self.log("unchanged", kind, label)
        return obj

    def _orphan(self, kind, obj, label):
        """Object exists in NetBox (with our tag) but is no longer in the design."""
        tags = [t.slug for t in (obj.tags or [])]
        if TAG not in tags:
            return
        if self.prune:
            self.log("delete", kind, label)
            if self.apply:
                obj.delete()
        else:
            print(f"  ? orphan  {kind:10s} {label}  (not in design; use --prune to delete)")

    # ------------------------------------------------------------------ devices
    def sync_devices(self):
        nb, d, t = self.nb, self.d, self.tags
        sid = self.oid(self.site)
        have = {dev.name: dev for dev in nb.dcim.devices.filter(site_id=sid, limit=0)} if sid else {}
        self.device = {}
        for name, dev in d.devices.items():
            fields = {"device_type": self.oid(self.dtype.get(dev.model)),
                      "role": self.oid(self.dev_role.get(dev.role)),
                      "site": sid, "location": self.oid(self.location.get(dev.location)),
                      "status": "active", "local_context_data": dev.context or None, "tags": t}
            if dev.role != "firewall":
                fields["platform"] = self.oid(self.platform)
            obj = have.pop(name, None)
            self.device[name] = self._ensure_known("device", nb.dcim.devices, obj,
                                                   {"name": name, **fields}, fields, name)
        for name, obj in have.items():
            self._orphan("device", obj, name)

    # ------------------------------------------------------------------ interfaces
    def sync_interfaces(self):
        nb, d = self.nb, self.d
        self.iface = {}                      # (device, name) -> pynetbox record
        by_device = defaultdict(list)
        for (dev, _), i in d.interfaces.items():
            by_device[dev].append(i)

        for dev_name, wanted in by_device.items():
            dev = self.device.get(dev_name)
            if dev is None:
                for i in wanted:
                    self.log("create" if i.kind != "physical" else "update", "interface",
                             f"{dev_name} {i.name}")
                continue
            have = {i.name: i for i in nb.dcim.interfaces.filter(device_id=dev.id, limit=0)}

            # 1) create LAG / virtual interfaces that do not exist yet
            to_create = []
            for i in wanted:
                if i.name in have:
                    continue
                if i.kind == "physical":
                    self.fail(f"{dev_name}: physical port {i.name} missing in NetBox device type")
                    continue
                self.log("create", "interface", f"{dev_name} {i.name}")
                to_create.append({"device": dev.id, "name": i.name,
                                  "type": "lag" if i.kind == "lag" else "virtual",
                                  "tags": self.tags})
            if to_create and self.apply:
                for rec in nb.dcim.interfaces.create(to_create):
                    have[rec.name] = rec
            for name, rec in have.items():
                self.iface[(dev_name, name)] = rec

            # 2) set all attributes (bulk PATCH per device)
            updates = []
            for i in wanted:
                rec = have.get(i.name)
                if rec is None:
                    continue
                fields = {
                    "enabled": i.enabled, "description": i.description,
                    "mode": i.mode,
                    "untagged_vlan": self.oid(self.vlan.get(i.untagged)) if i.untagged else None,
                    "tagged_vlans": [self.oid(self.vlan[v]) for v in i.tagged],
                    "lag": self.oid(have.get(i.lag)) if i.lag else None,
                    "vrf": self.oid(self.vrf.get(i.vrf)) if i.vrf else None,
                    "mtu": i.mtu, "mgmt_only": i.mgmt_only,
                    "tags": self.tags,
                }
                changes = diff(rec, fields)
                if changes:
                    self.log("update", "interface", f"{dev_name} {i.name}", ", ".join(sorted(changes)))
                    updates.append({"id": rec.id, **changes})
                else:
                    self.log("unchanged", "interface", f"{dev_name} {i.name}")
            if updates and self.apply:
                try:
                    nb.dcim.interfaces.update(updates)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{dev_name}: interface update failed: {exc}")

            # 3) LAG / virtual interfaces we created earlier but are no longer designed
            wanted_names = {i.name for i in wanted}
            for name, rec in have.items():
                if name not in wanted_names and rec.type.value in ("lag", "virtual"):
                    self._orphan("interface", rec, f"{dev_name} {name}")

    # ------------------------------------------------------------------ IP addresses
    def sync_ips(self):
        nb, d = self.nb, self.d

        def key_of(rec):
            obj = rec.assigned_object
            dev = obj.device.name if obj is not None and hasattr(obj, "device") else None
            return (str(rec.address), rec.vrf.name if rec.vrf else None, dev,
                    obj.name if obj is not None else None)

        have = {key_of(r): r for r in nb.ipam.ip_addresses.filter(tag=TAG, limit=0)} if self.tags else {}
        for ip in d.ips:
            key = (ip.address, ip.vrf, ip.device, ip.interface)
            iface = self.iface.get((ip.device, ip.interface))
            fields = {"status": "active", "role": ip.role, "dns_name": ip.dns_name,
                      "description": ip.description, "tags": self.tags}
            label = f"{ip.address} {ip.device} {ip.interface}"
            obj = have.pop(key, None)
            payload = {"address": ip.address, "vrf": self.oid(self.vrf.get(ip.vrf)) if ip.vrf else None,
                       "assigned_object_type": "dcim.interface",
                       "assigned_object_id": self.oid(iface), **fields}
            if obj is None and iface is None:
                self.log("create", "ip", label)
                continue
            self.ip_obj = getattr(self, "ip_obj", {})
            self.ip_obj[key] = self._ensure_known("ip", nb.ipam.ip_addresses, obj, payload, fields, label)
        for key, obj in have.items():
            self._orphan("ip", obj, f"{key[0]} {key[2]} {key[3]}")

    # ------------------------------------------------------------------ cables
    def sync_cables(self):
        nb, d = self.nb, self.d

        def ends(cable):
            out = []
            for side in (cable.a_terminations, cable.b_terminations):
                for term in side:
                    obj = term.object if hasattr(term, "object") else term
                    out.append((obj.device.name, obj.name))
            return frozenset(out)

        have = {}
        if self.tags:
            for c in nb.dcim.cables.filter(tag=TAG, limit=0):
                have[ends(c)] = c
        for cab in d.cables:
            label = f"{cab.a[0]} {cab.a[1]} <-> {cab.b[0]} {cab.b[1]}"
            obj = have.pop(cab.key, None)
            if obj is not None:
                fields = {"type": cab.type, "label": cab.label, "status": "connected"}
                self._ensure_known("cable", nb.dcim.cables, obj, {}, fields, label)
                continue
            a, b = self.iface.get(cab.a), self.iface.get(cab.b)
            self.log("create", "cable", label)
            if not self.apply or a is None or b is None:
                continue
            try:
                nb.dcim.cables.create({
                    "a_terminations": [{"object_type": "dcim.interface", "object_id": a.id}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": b.id}],
                    "type": cab.type, "label": cab.label, "status": "connected",
                    "tags": self.tags})
            except Exception as exc:  # noqa: BLE001
                self.fail(f"cable {label}: {exc}")
        for key, obj in have.items():
            self._orphan("cable", obj, " <-> ".join(f"{dv} {nm}" for dv, nm in sorted(key)))

    # ------------------------------------------------------------------ primary IPs
    def sync_primary_ips(self):
        for name, dev in self.d.devices.items():
            rec = self.device.get(name)
            if rec is None or not dev.primary:
                continue
            iface, addr = dev.primary
            vrf = self.d.interfaces[(name, iface)].vrf
            ip = getattr(self, "ip_obj", {}).get((addr, vrf, name, iface))
            if ip is None:
                self.log("update", "device", name, "primary_ip4")
                continue
            rec = self.nb.dcim.devices.get(rec.id)
            self._ensure_known("primary-ip", self.nb.dcim.devices, rec, {},
                               {"primary_ip4": ip.id}, name)

    # ------------------------------------------------------------------ report
    def report(self):
        print("\n== Summary ==")
        print(f"  {'object':12s} {'create':>7s} {'update':>7s} {'delete':>7s} {'same':>7s}")
        for kind, c in sorted(self.stats.items()):
            print(f"  {kind:12s} {c['create']:7d} {c['update']:7d} {c['delete']:7d} {c['unchanged']:7d}")
        if self.errors:
            print(f"\n{len(self.errors)} error(s).")
        elif not self.apply:
            print("\nNothing was changed. Run 'apply' to make these changes.")
