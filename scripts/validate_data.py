"""Check the YAML files in data/ for mistakes. Does NOT need NetBox.

Runs in GitHub Actions on every push, so a broken YAML never reaches main.
Usage:  python scripts/validate_data.py
"""

import sys
from collections import Counter

from lablib import MGMT_VID, load_data


def dupes(values):
    return [v for v, n in Counter(values).items() if n > 1]


def main():
    d = load_data()
    errors = []
    cat = d["catalog"]

    site_slugs = {s["slug"] for s in d["sites"]}
    for v in dupes([s["slug"] for s in d["sites"]]):
        errors.append(f"sites.yml: duplicate slug '{v}'")
    for v in dupes([s["number"] for s in d["sites"]]):
        errors.append(f"sites.yml: duplicate number {v}")
    for s in d["sites"]:
        if not 1 <= int(s["number"]) <= 254:
            errors.append(f"sites.yml: {s['name']} number must be 1-254")

    role_slugs = {r["slug"] for r in cat["ipam_roles"]}
    for v in dupes([x["vid"] for x in d["vlans"]]):
        errors.append(f"standard_vlans.yml: duplicate vid {v}")
    for x in d["vlans"]:
        if not 1 <= int(x["vid"]) <= 254:
            errors.append(f"standard_vlans.yml: vid {x['vid']} must be 1-254")
        if x["role"] not in role_slugs:
            errors.append(f"standard_vlans.yml: vid {x['vid']} uses unknown role '{x['role']}'")
    if MGMT_VID not in {x["vid"] for x in d["vlans"]}:
        errors.append(f"standard_vlans.yml: management VLAN {MGMT_VID} is missing")

    mfr_slugs = {m["slug"] for m in cat["manufacturers"]}
    for t in cat["device_types"]:
        if t["manufacturer"] not in mfr_slugs:
            errors.append(f"catalog.yml: device type {t['slug']} uses unknown manufacturer")
    type_slugs = {t["slug"] for t in cat["device_types"]}
    dev_roles = {r["slug"] for r in cat["device_roles"]}

    for v in dupes([x["name"] for x in d["devices"]]):
        errors.append(f"devices.yml: duplicate device name '{v}'")
    for v in dupes([(x["site"], x["mgmt_host"]) for x in d["devices"]]):
        errors.append(f"devices.yml: duplicate mgmt_host {v[1]} in site {v[0]}")
    for x in d["devices"]:
        n = x["name"]
        if x["site"] not in site_slugs:
            errors.append(f"devices.yml: {n} uses unknown site '{x['site']}'")
        if x["type"] not in type_slugs:
            errors.append(f"devices.yml: {n} uses unknown type '{x['type']}'")
        if x["role"] not in dev_roles:
            errors.append(f"devices.yml: {n} uses unknown role '{x['role']}'")
        if not 1 <= int(x["mgmt_host"]) <= 254:
            errors.append(f"devices.yml: {n} mgmt_host must be 1-254")

    if errors:
        print(f"{len(errors)} problem(s) found:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print(f"OK: {len(d['sites'])} sites, {len(d['vlans'])} VLANs per site, "
          f"{len(d['devices'])} devices")


if __name__ == "__main__":
    main()
