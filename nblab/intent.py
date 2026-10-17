"""Load the intent files and device type definitions from the repository."""

from .config import DEVICETYPE_DIR, INTENT_DIR, load_yaml


def load_intent(site_file="site-lab01.yml"):
    return {
        "site": load_yaml(INTENT_DIR / site_file),
        "vlans": load_yaml(INTENT_DIR / "vlan-plan.yml"),
        "profiles": load_yaml(INTENT_DIR / "port-profiles.yml"),
        "standards": load_yaml(INTENT_DIR / "standards.yml"),
    }


def load_device_types():
    """{slug: devicetype-library dict} for every file in devicetypes/."""
    out = {}
    for path in sorted(DEVICETYPE_DIR.glob("*.yaml")):
        data = load_yaml(path)
        out[data["slug"]] = data
    return out


def device_type_ports(device_types):
    return {slug: [i["name"] for i in d.get("interfaces", [])] for slug, d in device_types.items()}


def build_from_repo():
    from .design import build
    return build(load_intent(), device_type_ports(load_device_types()))
