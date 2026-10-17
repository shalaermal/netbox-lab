"""Write every switch configuration to configs/<site>/ - rendered BY NETBOX.

The template lives in netbox/config-templates/aruba-cx.j2 and is a native
NetBox Config Template (assigned to the platform by 'apply', or synced from
git in the GUI workflow). This command only asks NetBox to render each device
(POST /api/dcim/devices/<id>/render-config/) and saves the result, so the files
in git are exactly what NetBox shows in the device's "Render Config" tab.
"""

from .config import CONFIG_DIR
from .snapshot import Snapshot


def render_device(nb, device_id):
    resp = nb.http_session.post(
        f"{nb.base_url}/dcim/devices/{device_id}/render-config/",
        headers={"Authorization": f"Bearer {nb.token}" if nb.token.startswith("nbt_")
                 else f"Token {nb.token}", "Accept": "text/plain"},
        json={})
    if resp.status_code != 200:
        raise RuntimeError(f"render-config failed ({resp.status_code}): {resp.text[:500]}")
    return resp.text


def render_all(nb, force=False, only=None):
    from .checks import run_checks

    s = Snapshot(nb)
    print("Running checks first ...")
    if not run_checks(nb, snap=s) and not force:
        print("\nNot rendering: fix the errors above (or use --force).")
        return False

    out_dir = CONFIG_DIR / s.site.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for name in sorted(s.devices):
        if not s.is_switch(name) or (only and name != only):
            continue
        try:
            text = render_device(nb, s.devices[name].id)
        except RuntimeError as exc:
            print(f"  ! {name}: {exc}")
            return False
        (out_dir / f"{name}.cfg").write_text(text)
        count += 1
    print(f"\nRendered {count} config(s) into {out_dir.relative_to(CONFIG_DIR.parent)}/")
    return True
