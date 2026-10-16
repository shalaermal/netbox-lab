"""Shared helpers for the lab scripts: settings, NetBox connection, YAML data."""

import os
import sys
from pathlib import Path

import yaml

REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_DIR / "data"

TAG_SLUG = "lab-managed"  # every object the scripts create gets this tag
MGMT_VID = 99             # VLAN used for management IPs


def load_env():
    """Read KEY=VALUE lines from .env into os.environ (without overwriting)."""
    env_file = REPO_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connect():
    """Return a pynetbox API object. Works with old (40 hex) and new (nbt_...) tokens."""
    import pynetbox

    load_env()
    url = os.environ.get("NETBOX_URL", "http://localhost:8000")
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token == "PASTE_TOKEN_HERE":
        sys.exit("NETBOX_TOKEN is missing. Create a token in NetBox and put it in .env")

    # pynetbox >= 7.6 sends "Bearer" for v2 tokens (nbt_...) and "Token" for old ones
    nb = pynetbox.api(url, token=token)

    try:
        nb.status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Cannot reach NetBox at {url}: {exc}")
    return nb


def load_yaml(name):
    with open(DATA_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def load_data():
    return {
        "sites": load_yaml("sites.yml"),
        "vlans": load_yaml("standard_vlans.yml"),
        "catalog": load_yaml("catalog.yml"),
        "devices": load_yaml("devices.yml"),
    }


def prefix_for(site_number, vid):
    return f"10.{site_number}.{vid}.0/24"


def mgmt_ip_for(site_number, host):
    return f"10.{site_number}.{MGMT_VID}.{host}/24"
