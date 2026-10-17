"""Build netbox/scripts/netbox_lab.py - the NetBox Custom Script module.

NetBox loads a script module as ONE file, so it cannot import the nblab
package. This tool stitches the shared code into a single module:

    nblab/design.py           design engine      (shared with the CLI)
    nblab/checks.py           validation rules   (shared with the CLI)
    intent/*.yml, devicetypes/*.yaml   embedded as the form defaults
    netbox/scripts/src/orm.py          ORM writer + reader (NetBox only)
    netbox/scripts/src/scripts.py      the Script classes  (NetBox only)

Run it after changing any of those files:   python tools/bundle_scripts.py
CI fails if the committed bundle is out of date (tests/test_bundle.py).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "netbox" / "scripts" / "netbox_lab.py"


def strip_docstring(src):
    return re.sub(r'\A\s*""".*?"""\n', "", src, count=1, flags=re.S)


def section(title, body):
    bar = "# " + "=" * 75
    return f"\n\n{bar}\n# {title}\n{bar}\n{body.strip()}\n"


def build_bundle():
    design = strip_docstring((REPO / "nblab/design.py").read_text())

    checks = strip_docstring((REPO / "nblab/checks.py").read_text())
    checks = checks.replace("from .design import vlan_ranges\n", "")
    # ipaddress / re are already imported by the design section
    checks = checks.replace("import ipaddress\nimport re\n", "")
    # the CLI entry point reads NetBox over the REST API; not needed inside NetBox
    checks = re.sub(r"\ndef run_checks\(.*?\n(?=def evaluate)", "\n", checks, flags=re.S)
    checks = re.sub(r"\ndef _print\(r\):.*\Z", "\n", checks, flags=re.S)

    intent = {name: (REPO / "intent" / f"{file}.yml").read_text()
              for name, file in (("site", "site-lab01"), ("vlans", "vlan-plan"),
                                 ("profiles", "port-profiles"), ("standards", "standards"))}
    dtypes = {}
    for path in sorted((REPO / "devicetypes").glob("*.yaml")):
        text = path.read_text()
        slug = re.search(r"^slug:\s*(\S+)", text, re.M).group(1)
        dtypes[slug] = text
    data = "DEFAULT_YAML = {\n" + "".join(
        f"    {k!r}: {v!r},\n" for k, v in intent.items()) + "}\n\n"
    data += "DEFAULT_DEVICE_TYPES = {\n" + "".join(
        f"    {k!r}: {v!r},\n" for k, v in dtypes.items()) + "}\n"

    orm = (REPO / "netbox/scripts/src/orm.py").read_text()
    scripts = (REPO / "netbox/scripts/src/scripts.py").read_text()

    header = '''"""netbox-lab: Build site / Validate site / Reset site (NetBox Custom Scripts).

GENERATED FILE - DO NOT EDIT. Edit nblab/*.py, intent/*.yml, devicetypes/*.yaml
or netbox/scripts/src/*.py, then run:  python tools/bundle_scripts.py
"""
'''
    return (header
            + section("Design engine (from nblab/design.py)", design)
            + section("Validation rules (from nblab/checks.py)", checks)
            + section("Default design rules (from intent/*.yml and devicetypes/*.yaml)", data)
            + section("NetBox database layer (from netbox/scripts/src/orm.py)", orm)
            + section("Scripts (from netbox/scripts/src/scripts.py)", scripts))


def main():
    text = build_bundle()
    compile(text, str(OUT), "exec")
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(REPO)} is out of date: run python tools/bundle_scripts.py")
            return 1
        print("bundle is up to date")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(REPO)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
