"""The NetBox Custom Script and the Config Template must stay in sync with the code."""

import ast
import sys
from pathlib import Path

import jinja2

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import bundle_scripts  # noqa: E402


def test_bundle_is_up_to_date():
    assert bundle_scripts.OUT.read_text() == bundle_scripts.build_bundle(), \
        "run: python tools/bundle_scripts.py"


def test_bundle_defines_the_scripts():
    tree = ast.parse(bundle_scripts.OUT.read_text())
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert {"BuildSite", "ValidateSite", "ResetSite"} <= classes


def test_config_template_syntax():
    src = (REPO / "netbox/config-templates/aruba-cx.j2").read_text()
    jinja2.Environment(trim_blocks=True, lstrip_blocks=True).parse(src)


def test_no_name_clash_between_design_and_django_models():
    """design.py has Device/Interface/Cable/Prefix dataclasses; the ORM code must
    import Django models under other names or build() would use the wrong class."""
    tree = ast.parse(bundle_scripts.OUT.read_text())
    bound = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {a.asname or a.name for a in node.names}
    assert not bound & {"Device", "Interface", "Cable", "Prefix", "Vlan", "IP"}


def test_script_vars_do_not_shadow_script_attributes():
    """A form variable called e.g. 'description' hides the script's own description
    and breaks NetBox's script list API (HTTP 500)."""
    reserved = {"name", "description", "full_name", "module", "fieldsets", "field_order",
                "job_timeout", "commit_default", "scheduling_enabled", "class_name"}
    tree = ast.parse(bundle_scripts.OUT.read_text())
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        if not any(getattr(b, "id", "") == "Script" for b in cls.bases):
            continue
        for node in cls.body:
            if isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                assert not names & reserved, f"{cls.name}: {names & reserved}"
