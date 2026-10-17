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
