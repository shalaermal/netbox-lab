"""Paths, constants and the NetBox connection."""

import os
import sys
from pathlib import Path

import yaml

REPO_DIR = Path(__file__).resolve().parent.parent
INTENT_DIR = REPO_DIR / "intent"
DEVICETYPE_DIR = REPO_DIR / "devicetypes"
CONFIG_DIR = REPO_DIR / "configs"

TAG = "lab-managed"          # every object created by the code gets this tag
PLATFORM = {"name": "Aruba AOS-CX", "slug": "aruba-aoscx"}
CONFIG_TEMPLATE = {
    "name": "Aruba CX",
    "path": REPO_DIR / "netbox" / "config-templates" / "aruba-cx.j2",
    "environment_params": {"trim_blocks": True, "lstrip_blocks": True},
}


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


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
    """Return a pynetbox API object (v2 'nbt_' tokens and old tokens both work)."""
    import pynetbox

    load_env()
    url = os.environ.get("NETBOX_URL", "http://localhost:8000")
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token == "PASTE_TOKEN_HERE":
        sys.exit("NETBOX_TOKEN is missing. Create a token in NetBox and put it in .env")
    nb = pynetbox.api(url, token=token)
    try:
        nb.status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Cannot reach NetBox at {url}: {exc}")
    return nb
