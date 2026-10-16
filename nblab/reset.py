"""Delete everything tagged 'lab-managed' (children first). Hand-made objects stay."""

from .config import TAG


def reset(nb, yes):
    if not yes:
        print("This deletes every object tagged 'lab-managed'. Run again with --yes")
        return 1
    order = [
        ("cables", nb.dcim.cables), ("ip addresses", nb.ipam.ip_addresses),
        ("devices", nb.dcim.devices), ("prefixes", nb.ipam.prefixes),
        ("vlans", nb.ipam.vlans), ("vlan groups", nb.ipam.vlan_groups),
        ("locations", nb.dcim.locations), ("sites", nb.dcim.sites),
        ("device types", nb.dcim.device_types), ("device roles", nb.dcim.device_roles),
        ("platforms", nb.dcim.platforms), ("manufacturers", nb.dcim.manufacturers),
        ("ipam roles", nb.ipam.roles), ("vrfs", nb.ipam.vrfs),
    ]
    for ctx in nb.extras.config_contexts.filter(q="standards-"):
        ctx.delete()
        print("  - deleted config context", ctx.name)
    for label, ep in order:
        objs = list(ep.filter(tag=TAG, limit=0))
        if objs:
            ep.delete(objs)
            print(f"  - deleted {len(objs):4d} {label}")
    print("Done. Run 'python -m nblab apply' to build everything again.")
    return 0
