"""Delete everything the lab scripts created (objects tagged "lab-managed").

Objects you created by hand WITHOUT the tag are not touched.
Custom fields and the tag itself are kept.

Usage:  python scripts/reset_lab.py --yes
"""

import argparse
import sys

from lablib import TAG_SLUG, connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="really delete")
    args = parser.parse_args()
    if not args.yes:
        sys.exit("This deletes all objects tagged 'lab-managed'. Run again with --yes")

    nb = connect()
    # Order matters: children first, parents last
    endpoints = [
        nb.ipam.ip_addresses,
        nb.dcim.devices,          # also deletes their interfaces
        nb.ipam.prefixes,
        nb.ipam.vlans,
        nb.ipam.vlan_groups,
        nb.dcim.sites,
        nb.dcim.device_types,
        nb.dcim.device_roles,
        nb.dcim.manufacturers,
        nb.ipam.roles,
    ]
    total = 0
    for ep in endpoints:
        objs = list(ep.filter(tag=TAG_SLUG))
        for obj in objs:
            obj.delete()
        if objs:
            print(f"  - deleted {len(objs):3d} {ep.name}")
        total += len(objs)
    print(f"Done: {total} objects deleted.")


if __name__ == "__main__":
    main()
