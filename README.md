# NetBox Lab: from design to switch configs

A personal lab for the full lifecycle of a new campus network, the way teams
that use a **source of truth** work:

```
 design decisions ──►  NetBox  ──►  validation  ──►  switch configs
  (form or YAML)     (the truth)     (11 rules)      (Aruba AOS-CX)
```

1. You describe the **design as rules**: floors, switches per floor, VLAN plan,
   port profiles, standards.
2. Code turns the rules into a **complete NetBox**: devices, ports, cables,
   LAGs / MC-LAGs, VSX, VLANs, prefixes, IP addresses.
3. **Validation** checks what is in NetBox: dual-homed access, same VLANs on
   both ends of every link, symmetric MC-LAGs, gateways, MTU, duplicate IPs.
4. **NetBox renders the configs** from its own data with a native Config
   Template. Change something in NetBox, and the config changes with it.

All data is **fictional**. Never put real IPs, names or secrets from work here.

There are two ways to drive it, both using the **same** design engine, rules
and template:

| | GUI workflow (start here) | Git workflow (later) |
|---|---|---|
| Build a site | *Customization → Scripts → Build site*, fill the form | edit `intent/*.yml`, `python -m nblab apply` |
| Preview | untick *Commit changes* | `python -m nblab plan` |
| Small changes | edit directly in NetBox | edit YAML + `apply` |
| Validate | *Scripts → Validate site* | `python -m nblab check` |
| Config | device → **Render Config** tab | `python -m nblab render` → `configs/` |
| History | NetBox changelog + job results | git commits / pull requests |

---

## The example project: LAB01

| | |
|---|---|
| Topology | 2 core (VSX) → 2 aggregation (VSX) → 44 access, dual-homed with MC-LAG. HA firewall pair. |
| Devices | **50**: Aruba 8360-48Y6C (core/agg), Aruba 6300M-48G (access), fictional firewall |
| VLANs | **62**: 8 functions × 6 floors + 12 site-wide + transit + parking |
| Routing | OSPF area 0 core ↔ agg (/31), VSX active-gateway on agg, default route to the firewall |
| Cabling | 102 cables, 142 LAGs |

### IP plan (rule: VLAN ID = third octet)

| What | Where | Example |
|---|---|---|
| Floor VLANs | `vid = 100 + 10 × floor + function` | Floor 1 Users = VLAN 110 = `10.1.110.0/24` |
| Site VLANs | VLAN 20–99 | WiFi-Corp = VLAN 20 = `10.1.20.0/24` |
| Gateway in every /24 | `.1` VIP, `.2` AGG-01, `.3` AGG-02 | `10.1.110.1` |
| Firewall transit | VLAN 900, `10.1.250.0/29` | `.1` FW, `.2/.3` core, `.4` core VIP |
| P2P core-agg | `10.1.251.0/24` as /31s | |
| VSX keepalive | `10.1.252.0/24`, VRF KA | |
| OOB management | `10.1.253.0/24`, VRF mgmt | |
| Loopbacks | `10.1.254.x/32` | router ID |
| Access switch mgmt | VLAN 99, `10.1.99.101` and up | |

### Best practices built in
- **VSX** on core and aggregation: ISL `lag 256`, keepalive on a dedicated port in VRF `KA`,
  unique system MAC per pair.
- **Dual-homed access** with MC-LAG: no STP-blocked uplinks.
- **Edge ports**: `admin-edge`, `bpdu-guard`, `loop-protect`. Aggregation MC-LAGs have
  `root-guard`; aggregation is the root bridge.
- **DHCP snooping** on access (trusted only on the uplink), **DHCP relay** only on client VLANs.
- **VLAN pruning**: every access switch carries only its floor's VLANs plus the site-wide ones.
- **Unused ports are shut down**; unused access ports sit in VLAN 999 (Parking).
- **Separate management**: core/agg out-of-band (VRF mgmt), access in-band (VLAN 99).
- **Jumbo MTU** on infrastructure links, configured on the LAG (as AOS-CX requires).
- **No secrets in git**: configs contain `<<TACACS_KEY>>`, `<<SNMP_AUTH_PASS>>` etc.
- **Preview before change**, and running twice changes nothing (idempotent).

---

## Start the lab (GitHub Codespaces, nothing to install)

1. **Code → Codespaces → Create codespace on main**. The first start takes 5–10 minutes;
   NetBox starts by itself. Wait for `NetBox is up` in the terminal.
2. **Ports** tab → port **8000** → globe icon. Log in with **admin / netboxlab**.
3. In NetBox: **admin (top right) → API Tokens → Add a Token** (*Write enabled*). Paste it
   into `.env` as `NETBOX_TOKEN=...`.
4. Install the GUI tools into NetBox (Custom Scripts + Config Template), once:
   ```bash
   python -m nblab setup-gui
   ```

When you are done: blue box bottom-left → **Stop Current Codespace**.

---

## GUI workflow

### Build a site
1. **Customization → Scripts → netbox_lab → Build site**.
2. The form is pre-filled with the LAB01 design. Change what you need:
   - **Site name / number** (`LAB02`, `2` → all addresses become `10.2.x.x`)
   - **Access switches per floor**: `7,7,7,7,8,8` = 6 floors
   - **Models**: leave empty for the Aruba defaults (imported automatically)
   - **Design rules (YAML, advanced)**: port plan, VLAN plan, port profiles, standards
3. Leave **Commit changes** unticked and click **Run script** → a preview: what would be
   created, and the validation result. Nothing is saved.
4. Tick **Commit changes** and run again → the site is built (about 1 minute).

### Look at the result
- **Devices → Devices**, filter by site: every switch, its ports, LAGs, cables, IPs.
- Open a switch → **Render Config** tab: the full Aruba CX configuration, generated live
  from NetBox. Copy or download it.
- **IPAM → VLANs / Prefixes**: the whole VLAN and IP plan.

### Change something
Edit directly in NetBox, for example move a port to another VLAN, add a server with an
MC-LAG to both aggregation switches, or change a description. Then:
1. **Scripts → Validate site** → check nothing is broken.
2. Open the switch → **Render Config**: the change is already in the config.

### Practice
- **Scripts → Reset site (lab only)** deletes everything the scripts created for a site
  (objects tagged `lab-managed`); hand-made objects stay.

### Install by hand (instead of `setup-gui`)
- **Customization → Scripts → Add** → upload `netbox/scripts/netbox_lab.py`.
- **Provisioning → Config Templates → Add** → name `Aruba CX`, paste
  `netbox/config-templates/aruba-cx.j2`, environment params
  `{"trim_blocks": true, "lstrip_blocks": true}`.
- **Devices → Platforms → Aruba AOS-CX** → Config template = `Aruba CX`.

Optional, to keep NetBox in sync with git: **Operations → Data Sources → Add**
(type *Local*, URL `file:///opt/netbox-lab` inside the Codespace, or type *git* with the
repo URL), then point the Config Template at `netbox/config-templates/aruba-cx.j2` from
that source. After a `git pull`, click **Sync**.

---

## Git workflow

```bash
python -m nblab design     # what the intent produces (offline, no NetBox)
python -m nblab plan       # compare with NetBox, change nothing
python -m nblab apply      # make NetBox match the design (first time ~5 minutes)
python -m nblab check      # validate the data in NetBox
python -m nblab render     # NetBox renders every switch → configs/lab01/*.cfg
pytest -q                  # design rules + bundle checks, no NetBox needed
```

More:
```bash
python -m nblab apply --prune   # also delete lab objects that left the design
python -m nblab render --device LAB01-AGG-01
python -m nblab reset --yes     # delete everything tagged lab-managed
```

The design lives in `intent/`. These are the only files you edit by hand:

| File | What you decide there |
|---|---|
| `intent/site-lab01.yml` | site, floors and access switches per floor, models, physical port plan, VSX |
| `intent/vlan-plan.yml` | VLAN functions per floor, site-wide VLANs, infrastructure ranges |
| `intent/port-profiles.yml` | port profiles (user, ap, printer…) and which ports get which profile |
| `intent/standards.yml` | NTP, DNS, syslog, TACACS, DHCP relay, OSPF, STP, MTU |

`configs/` is committed on purpose: in a pull request every design change shows up as a
config diff.

---

## Repository layout

```
intent/                     design rules (YAML)                       ← git workflow input
devicetypes/                Aruba models from devicetype-library (real port lists)
nblab/
  design.py                 rules → complete design (pure Python, no NetBox, no files)
  checks.py                 the validation rules (shared by CLI and GUI)
  intent.py                 loads intent/ and devicetypes/
  sync.py                   plan / apply over the REST API
  snapshot.py               reads one site over the REST API (for checks)
  render.py                 asks NetBox to render each switch, saves configs/
  setup_gui.py              installs the GUI tools into NetBox
netbox/
  config-templates/aruba-cx.j2   the NetBox Config Template (single file)
  scripts/src/orm.py             database layer used inside NetBox
  scripts/src/scripts.py         Build site / Validate site / Reset site
  scripts/netbox_lab.py          GENERATED single-file module NetBox loads
tools/bundle_scripts.py     builds netbox/scripts/netbox_lab.py
configs/lab01/              rendered configs (by NetBox)
tests/                      run in GitHub Actions on every push
```

NetBox loads a Custom Script as **one file**, so `tools/bundle_scripts.py` stitches
`design.py`, `checks.py`, the default intent and the ORM layer into
`netbox/scripts/netbox_lab.py`. After changing any of them:
```bash
python tools/bundle_scripts.py && python -m nblab setup-gui
```
CI fails if the bundle is out of date.

---

## What changes automatically, and what needs template work

**Automatic** (it is data in NetBox): which ports connect where, LAG / MC-LAG members, VLANs
on any port or trunk, IPs, gateways, VSX roles, descriptions, MTU, enabled/disabled.

**Needs a one-time template change** (a new *feature*): BGP, EVPN-VXLAN, ACLs, QoS,
802.1X, another vendor. Add it once to `aruba-cx.j2` and every switch can use it.

---

## Exercises

1. **Read a config.** In the GUI open `LAB01-AGG-01` → *Render Config*. Find the MC-LAG to
   `F1-ACC-01`, the gateway of VLAN 110 and the VSX keepalive. Then find the same things in
   the device's *Interfaces* and *IP Addresses*.
2. **Build a second site.** *Build site* with `LAB02`, number `2`, floors `4,4`. Preview,
   then commit. Compare a LAB02 access config with a LAB01 one.
3. **Break it on purpose.** On `LAB01-AGG-02`, remove VLAN 113 from `lag5`. Run
   *Validate site*: two errors (trunk mismatch, asymmetric MC-LAG). Fix it, validate again.
4. **A one-off change.** Give `LAB01-F3-ACC-02` port `1/1/41` the camera VLAN instead of
   the printer VLAN, directly in NetBox. Check *Render Config*.
5. **Add a server.** Create a device `LAB01-SRV-01`, cable it to `1/1/46` on both AGG
   switches, and create `lag60` on both with those members and the Servers VLAN. Validate,
   then look at both AGG configs.
6. **Git workflow.** Add `{floor: 7, access_switches: 3}` to `intent/site-lab01.yml`, run
   `plan`, `apply`, `render`, and read `git diff configs/`.
7. **New rule.** Add a rule to `nblab/checks.py`, for example "every enabled access port has
   a description". Rebuild the bundle, `setup-gui`, run *Validate site*.

## Next steps
- **Deploy**: Nornir + Scrapli/NAPALM with a diff before commit (e.g. against EVE-NG).
- **802.1X / MAC auth** (port-access) instead of static port profiles.
- **Monitoring**: Zabbix fed from NetBox with `netbox-zabbix-sync`.
