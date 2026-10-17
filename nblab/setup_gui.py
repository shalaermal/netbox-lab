"""Install the GUI tools into NetBox (one command instead of clicking).

    1. uploads netbox/scripts/netbox_lab.py   -> Customization > Scripts
       (Build site, Validate site, Reset site)
    2. uploads netbox/config-templates/aruba-cx.j2 -> Provisioning > Config Templates
       and assigns it to the "Aruba AOS-CX" platform

Run it again after a `git pull` to update both. The same can be done by hand in
the GUI (see README, "GUI workflow -> install by hand").
"""

from .config import CONFIG_TEMPLATE, PLATFORM, REPO_DIR

SCRIPT_FILE = REPO_DIR / "netbox" / "scripts" / "netbox_lab.py"


def _headers(nb, accept="application/json"):
    auth = f"Bearer {nb.token}" if nb.token.startswith("nbt_") else f"Token {nb.token}"
    return {"Authorization": auth, "Accept": accept}


def upload_script_module(nb):
    """POST /api/extras/scripts/upload/ (new) or PATCH .../upload/<module id>/ (update)."""
    resp = nb.http_session.get(f"{nb.base_url}/extras/scripts/", headers=_headers(nb),
                               params={"limit": 0})
    resp.raise_for_status()
    module_id = next((s["module"] for s in resp.json()["results"]
                      if s["module"] and s["name"] in ("BuildSite", "ValidateSite", "ResetSite")),
                     None)
    if isinstance(module_id, dict):
        module_id = module_id.get("id")
    url = f"{nb.base_url}/extras/scripts/upload/"
    with open(SCRIPT_FILE, "rb") as fh:
        files = {"file": (SCRIPT_FILE.name, fh, "text/x-python")}
        if module_id:
            r = nb.http_session.patch(f"{url}{module_id}/", headers=_headers(nb), files=files)
        else:
            r = nb.http_session.post(url, headers=_headers(nb), files=files)
    if r.status_code not in (200, 201):
        raise SystemExit(f"Script upload failed ({r.status_code}): {r.text[:800]}")
    print(f"  {'updated' if module_id else 'created'} script module {SCRIPT_FILE.name}")


def upload_config_template(nb):
    code = CONFIG_TEMPLATE["path"].read_text().rstrip("\n")
    ct = nb.extras.config_templates.get(name=CONFIG_TEMPLATE["name"])
    fields = {"template_code": code, "environment_params": CONFIG_TEMPLATE["environment_params"],
              "description": "Aruba AOS-CX switch configuration (netbox-lab)"}
    if ct is not None and ct.data_source:
        print(f"  config template '{ct.name}' is synced from a data source: not overwritten "
              "(use the Sync button in NetBox)")
    elif ct is None:
        ct = nb.extras.config_templates.create(name=CONFIG_TEMPLATE["name"], **fields)
        print(f"  created config template '{ct.name}'")
    else:
        ct.update(fields)
        print(f"  updated config template '{ct.name}'")
    plat = nb.dcim.platforms.get(slug=PLATFORM["slug"])
    if plat is None:
        plat = nb.dcim.platforms.create(name=PLATFORM["name"], slug=PLATFORM["slug"],
                                        config_template=ct.id)
        print(f"  created platform '{plat.name}' using the template")
    elif not plat.config_template or plat.config_template.id != ct.id:
        plat.update({"config_template": ct.id})
        print(f"  platform '{plat.name}' now uses the template")


def setup_gui(nb):
    print("Installing the GUI tools into NetBox ...")
    upload_script_module(nb)
    upload_config_template(nb)
    print("\nDone. In NetBox open: Customization > Scripts > netbox_lab > Build site")
    return 0
