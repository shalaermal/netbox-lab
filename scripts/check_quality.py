"""Data quality report for NetBox. Checks the WHOLE NetBox, not only lab objects.

Checks:
  1. active devices without a primary IPv4
  2. devices whose device type has no zabbix_template
  3. prefixes without a VLAN
  4. IP addresses without a DNS name
  5. devices with no site-specific naming (name does not start with the site name)

Usage:  python scripts/check_quality.py           # report only
        python scripts/check_quality.py --strict  # exit code 1 if problems found
"""

import argparse
import sys

from lablib import connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="exit with code 1 when problems are found")
    args = parser.parse_args()

    nb = connect()
    problems = {}

    devices = list(nb.dcim.devices.filter(status="active"))
    templates = {t.id: (t.custom_fields or {}).get("zabbix_template")
                 for t in nb.dcim.device_types.all()}

    problems["Active devices without primary IPv4"] = [
        d.name for d in devices if not d.primary_ip4]
    problems["Devices whose type has no zabbix_template"] = [
        f"{d.name} ({d.device_type.model})" for d in devices
        if not templates.get(d.device_type.id)]
    problems["Devices not named <SITE>-..."] = [
        f"{d.name} (site {d.site.name})" for d in devices
        if d.name and not d.name.upper().startswith(d.site.name.upper() + "-")]
    problems["Prefixes without VLAN"] = [
        str(p.prefix) for p in nb.ipam.prefixes.all() if not p.vlan]
    problems["IP addresses without DNS name"] = [
        str(ip.address) for ip in nb.ipam.ip_addresses.all() if not ip.dns_name]

    total = 0
    for title, items in problems.items():
        mark = "OK " if not items else "!! "
        print(f"{mark}{title}: {len(items)}")
        for item in items[:20]:
            print(f"     - {item}")
        if len(items) > 20:
            print(f"     ... and {len(items) - 20} more")
        total += len(items)

    print(f"\nTotal problems: {total}")
    if args.strict and total:
        sys.exit(1)


if __name__ == "__main__":
    main()
