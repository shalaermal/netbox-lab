"""Build the whole lab in NetBox from the YAML files in data/.

Safe to run many times: existing objects are updated, missing ones are created
(this is called "idempotent"). Every object gets the tag "lab-managed".

Usage:  python scripts/build_lab.py
"""

from collections import Counter

from lablib import (MGMT_VID, TAG_SLUG, connect, load_data, mgmt_ip_for,
                    prefix_for)

stats = Counter()

# Filters use "site_id", but when creating, the API field is called "site"
FILTER_TO_FIELD = {"site_id": "site", "group_id": "group", "device_id": "device"}


def ensure(endpoint, lookup, fields=None, label=None):
    """Get the object matching `lookup`; create it if missing, else update `fields`."""
    fields = fields or {}
    label = label or str(lookup)
    obj = endpoint.get(**lookup)
    if obj is None:
        create = {FILTER_TO_FIELD.get(k, k): v for k, v in lookup.items()}
        obj = endpoint.create({**create, **fields})
        stats["created"] += 1
        print(f"  + created  {endpoint.name}: {label}")
    elif fields and obj.update(fields):
        stats["updated"] += 1
        print(f"  ~ updated  {endpoint.name}: {label}")
    else:
        stats["unchanged"] += 1
    return obj


def main():
    nb = connect()
    d = load_data()
    cat = d["catalog"]

    print("Tag and custom fields")
    tag = ensure(nb.extras.tags, {"slug": TAG_SLUG},
                 {"name": TAG_SLUG, "color": "9e9e9e",
                  "description": "Created by netbox-lab scripts"})
    tags = [tag.id]
    ensure(nb.extras.custom_fields, {"name": "zabbix_hostid"},
           {"type": "integer", "label": "Zabbix host ID",
            "object_types": ["dcim.device", "virtualization.virtualmachine"]})
    ensure(nb.extras.custom_fields, {"name": "zabbix_template"},
           {"type": "text", "label": "Zabbix template",
            "object_types": ["dcim.devicetype"]})

    print("Catalog")
    ipam_roles = {r["slug"]: ensure(nb.ipam.roles, {"slug": r["slug"]},
                                    {"name": r["name"], "tags": tags})
                  for r in cat["ipam_roles"]}
    mfrs = {m["slug"]: ensure(nb.dcim.manufacturers, {"slug": m["slug"]},
                              {"name": m["name"], "tags": tags})
            for m in cat["manufacturers"]}
    dtypes = {t["slug"]: ensure(nb.dcim.device_types, {"slug": t["slug"]},
                                {"model": t["model"],
                                 "manufacturer": mfrs[t["manufacturer"]].id,
                                 "custom_fields": {"zabbix_template": t.get("zabbix_template")},
                                 "tags": tags})
              for t in cat["device_types"]}
    droles = {r["slug"]: ensure(nb.dcim.device_roles, {"slug": r["slug"]},
                                {"name": r["name"], "color": r["color"], "tags": tags})
              for r in cat["device_roles"]}

    sites = {}
    mgmt_vlans = {}
    for s in d["sites"]:
        print(f"Site {s['name']}")
        site = ensure(nb.dcim.sites, {"slug": s["slug"]},
                      {"name": s["name"], "status": "active",
                       "description": s.get("description", ""), "tags": tags})
        sites[s["slug"]] = (site, s["number"])

        group = ensure(nb.ipam.vlan_groups, {"slug": s["slug"]},
                       {"name": f"{s['name']} VLANs", "scope_type": "dcim.site",
                        "scope_id": site.id, "tags": tags})

        for v in d["vlans"]:
            vlan = ensure(nb.ipam.vlans, {"group_id": group.id, "vid": v["vid"]},
                          {"name": v["name"], "status": "active", "tags": tags},
                          label=f"{s['name']} VLAN {v['vid']} {v['name']}")
            if v["vid"] == MGMT_VID:
                mgmt_vlans[s["slug"]] = vlan
            pfx = prefix_for(s["number"], v["vid"])
            ensure(nb.ipam.prefixes, {"prefix": pfx},
                   {"status": "active", "vlan": vlan.id,
                    "role": ipam_roles[v["role"]].id,
                    "scope_type": "dcim.site", "scope_id": site.id,
                    "description": f"{s['name']} {v['name']}", "tags": tags})

    print("Devices")
    for x in d["devices"]:
        site, number = sites[x["site"]]
        dev = ensure(nb.dcim.devices, {"name": x["name"], "site_id": site.id},
                     {"device_type": dtypes[x["type"]].id,
                      "role": droles[x["role"]].id,
                      "status": "active", "tags": tags},
                     label=x["name"])
        iface = ensure(nb.dcim.interfaces, {"device_id": dev.id, "name": "mgmt0"},
                       {"type": "virtual", "mgmt_only": True,
                        "description": "Management", "tags": tags},
                       label=f"{x['name']} mgmt0")
        address = mgmt_ip_for(number, x["mgmt_host"])
        ip = ensure(nb.ipam.ip_addresses, {"address": address},
                    {"status": "active",
                     "assigned_object_type": "dcim.interface",
                     "assigned_object_id": iface.id,
                     "dns_name": f"{x['name'].lower()}.lab.local",
                     "tags": tags},
                    label=f"{address} -> {x['name']}")
        dev = nb.dcim.devices.get(dev.id)  # refresh before setting primary IP
        if dev.update({"primary_ip4": ip.id}):
            print(f"  ~ primary IP of {x['name']} = {address}")

    print(f"\nDone: {stats['created']} created, {stats['updated']} updated, "
          f"{stats['unchanged']} unchanged.")


if __name__ == "__main__":
    main()
