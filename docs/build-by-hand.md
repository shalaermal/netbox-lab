# Build any design by hand in NetBox

*Build site* creates one reference architecture. For anything else (four
aggregation switches, a different core, special links) you enter the design in
NetBox yourself, through the GUI or CSV import. The config template does not
know or care how many switches there are: it reads whatever NetBox contains.

This guide has three parts:

1. [How a config is produced](#1-how-a-config-is-produced): which template, how it is linked
2. [The data contract](#2-the-data-contract): what you must enter so the template understands it
3. [Step by step](#3-step-by-step-with-csv-import), with tested CSV files in [`csv-examples/`](csv-examples/)

Tested against NetBox 4.7.3 with a hand-built site (2 core + **4 aggregation in
two VSX pairs** + access switches on each pair) and with CSV imports of every kind
shown below.

---

## 1. How a config is produced

```
Device ──► Config Template "Aruba CX" ──► rendered config
  │            (Jinja2, netbox/config-templates/aruba-cx.j2)
  └── reads: the device, its interfaces, IPs, VLANs, cables,
             its config context (standards + vsx), other devices in the site
```

**Which template a device uses.** NetBox picks the first one it finds:

1. the device's own *Config template* field
2. its **device role**'s config template
3. its **platform**'s config template ← this lab uses this one

`python -m nblab setup-gui` (or Build site) sets *Devices → Platforms →
Aruba AOS-CX → Config template = Aruba CX*. So **any device with platform
`Aruba AOS-CX` gets a config**, however it was created.

**Where you see the config.**

| Where | How |
|---|---|
| GUI | Device → **Render Config** tab (copy / download) |
| API | `POST /api/dcim/devices/<id>/render-config/` with `Accept: text/plain` |
| CLI | `python -m nblab render` writes every switch of the site to `configs/<site>/` |

All three call the same template, so the output is identical.

**The Custom Scripts are not needed for configs.** *Build site* only creates
data and *Validate site* only reads it. The config comes from the Config
Template alone.

---

## 2. The data contract

The template turns NetBox data into AOS-CX commands. These are the conventions it
relies on. Anything not listed (port numbers, how many switches, which port goes
where, VLAN IDs, subnets) is free.

### Device

| Field | Value | What it drives |
|---|---|---|
| Platform | `Aruba AOS-CX` | selects the template; also marks the device as a switch for the checks |
| Role (slug) | `core`, `agg`, `access` (and `firewall` for the firewall) | management VRF, STP priority, root-guard, edge-port protection, DHCP snooping/relay, OSPF, default routes (see below) |
| Location | any | `snmp-server system-location` |
| Primary IPv4 | the management IP | required by *Validate site* |
| Name | `<SITE>-CORE-01`, `<SITE>-AGG-01`, `<SITE>-F1-ACC-01`, `<SITE>-FW-01` | required by the naming rule in *Validate site* (the template does not care) |

What each role turns on:

| | core | agg | access |
|---|---|---|---|
| Management | OOB, VRF `mgmt` | OOB, VRF `mgmt` | in-band SVI, VRF default |
| Spanning tree | priority 0 | priority 0, `root-guard` on MC-LAGs | edge ports: `admin-edge`, `bpdu-guard`, `loop-protect` |
| DHCP | | relay (`ip helper-address`) on client VLANs | snooping; uplink LAG trusted |
| OSPF | routed links + loopback, `default-information originate` | routed links + loopback, SVIs passive | |
| Default route | to the firewall VIP | (learned by OSPF) | to the VIP in the same subnet as its management IP |

### Config context

Two kinds, both visible on the device's **Config Context** tab:

**Standards**, one context assigned to the site (*Customization → Config
Contexts*, weight 1000, *Sites = your site*):

```yaml
dns_servers:    [10.1.30.53, 10.1.30.54]
ntp_servers:    [10.1.30.123, 10.1.30.124]
syslog_servers: [10.1.30.60]
dhcp_servers:   [10.1.30.67, 10.1.30.68]   # relay targets
tacacs_servers: [10.1.30.49, 10.1.30.50]
snmp: {v3_user: netmon, location_prefix: LAB01}
ospf: {process: 1, area: 0.0.0.0}
spanning_tree: {mode: mstp, config_name: LAB01, config_revision: 1}
active_gateway_mac: "12:00:00:00:01:00"
access_mgmt_vlan: 99          # or a list [98, 99]: no DHCP relay on these VLANs
timezone: europe/berlin
banner: |
  Authorized access only.
```

Copy the one Build site created (`standards-lab01`) and change *Sites*.

**VSX**, as *local config context* on **each** VSX member (device → Edit →
*Local config context data*):

```json
{"vsx": {"role": "primary", "system_mac": "02:00:03:00:00:02", "peer": "LAB03-AGG-02"}}
```

The peer has `"role": "secondary"`, the **same** system MAC, and `"peer"` pointing
back. Every VSX pair needs its own system MAC.

### Interfaces

The template recognises interfaces by **name and type**:

| You create | Name / type | Becomes |
|---|---|---|
| OOB management | `mgmt` (exists from the device type), IP in VRF `mgmt` | `interface mgmt`, `ip static`, `default-gateway` = first IP of the subnet |
| LAG | `lag<N>`, type *LAG* | `interface lag N`; on a VSX member every LAG is `multi-chassis`, except `lag256` |
| VSX ISL | `lag256`, type *LAG*, 802.1Q mode *Tagged (All)* | `inter-switch-link lag 256` |
| LAG member | physical port with *Parent LAG* set | `lag N` under the port |
| L2 port | 802.1Q mode *Access* / *Tagged* / *Tagged (All)*, untagged + tagged VLANs | `vlan access`, `vlan trunk native/allowed` |
| Routed port | physical port with an IP address | `routing`, `ip address`; OSPF point-to-point if the cable goes to another Aruba switch and there is no VRF |
| VSX keepalive | physical port in VRF `KA` with a /31 | `vrf attach KA`; the peer address is derived from the /31 |
| Loopback | `loopback0`, type *Virtual*, /32 | `interface loopback 0` + OSPF; also the OSPF router ID |
| SVI | `vlan<VID>`, type *Virtual* | `interface vlan VID` with its IPs |
| Gateway (VIP) | an IP on the SVI with **role = VIP** | `active-gateway ip ...` (put the same VIP on both VSX members) |
| Shut down | *Enabled* unticked | `shutdown` |
| MTU | *MTU* field (on the LAG, not its members) | `mtu`, `ip mtu` |
| Description | *Description* | `description` |

### VLANs and prefixes

- Put the site's VLANs in a **VLAN group scoped to the site**. Only VLANs from
  such groups are declared (`vlan N` / `name`), and only those the switch uses.
- A VLAN called `Parking` is exempt from the uplink rule (for unused ports).
- Give each gateway VLAN a **prefix** linked to the VLAN (*Validate site* checks it).
- Every IP should fall inside a prefix of the site, in the **same VRF**: e.g. the
  keepalive /31s inside a prefix in VRF `KA`.

### Rules *Validate site* enforces

Naming · primary IP · access dual-homed to two different aggregation switches ·
same VLANs and MTU on both ends of every switch-to-switch cable · /31 on both ends
of routed links · VSX pairs consistent (MAC, roles, ISL, keepalive, identical
MC-LAGs) · every VLAN on an access uplink has a gateway (one VIP) on the switches
that uplink goes to · every VLAN used on an access port is carried on its uplink ·
trunks with no VLANs (warning) · VLANs from the site's group · prefixes for gateway
VLANs · no duplicate IPs · IPs inside a site prefix.

These work for **any number** of core / aggregation switches or VSX pairs.

---

## 3. Step by step, with CSV import

Order matters: things must exist before you can point at them.

| # | What | Where in the GUI |
|---|---|---|
| 1 | Site, locations | *Organization → Sites / Locations* |
| 2 | VLAN group (scope: the site), VLANs, prefixes, VRFs `mgmt` + `KA` | *IPAM* |
| 3 | Config context with the standards | *Customization → Config Contexts* |
| 4 | Devices (platform `Aruba AOS-CX`, the right role); VSX local context | *Devices → Devices* → **Import** |
| 5 | New interfaces: LAGs, SVIs, loopbacks | *Devices → Interfaces* → **Import** |
| 6 | Update the physical ports: LAG membership, VLAN mode, description | *Interfaces* → **Export** → edit → **Import** |
| 7 | Cables | *Devices → Cables* → **Import** |
| 8 | IP addresses (VIPs with role *VIP*), primary IPs | *IPAM → IP Addresses* → **Import** |
| 9 | **Scripts → Validate site** → fix → repeat | |
| 10 | Device → **Render Config** | |

Device types: import the real ones from
[devicetype-library](https://github.com/netbox-community/devicetype-library) (this
repo has the Aruba ones in `devicetypes/`, and Build site imports them
automatically). Creating a device then creates all its physical ports.

### Excel workbook: a whole site, ready to import

[`LAB20-netbox-import.xlsx`](LAB20-netbox-import.xlsx) builds a complete small site
(2 core, 2 aggregation, 10 access) with one sheet per Import page, in order, plus a
README sheet. Copy a sheet's cells and paste them into the Import box (tab-separated
data is detected automatically). Tested end to end: all sheets import, *Validate site*
reports 0 errors, every switch renders. Regenerate it with
`python tools/make_import_workbook.py docs/LAB20-netbox-import.xlsx`.

Two NetBox details it handles:
- On **interface** imports the VRF is looked up by RD, so the column is `vrf.name`
  (`<field>.<attribute>` works for any column).
- Updating existing ports needs their IDs: the `_ids` sheet takes the interface export
  and the `id` column of `08_ports_update` fills itself with `INDEX/MATCH`.

### CSV files

Every **Import** page accepts CSV pasted into the text box or uploaded. The files
in [`csv-examples/`](csv-examples/) add one access switch to the hand-built LAB03
site and were tested in that order.

**Devices**: [`1-devices.csv`](csv-examples/1-devices.csv)
```csv
name,role,manufacturer,device_type,platform,site,location,status
LAB03-F3-ACC-01,Access,HPE,Aruba 6300M-48G-POE4-4SFP56,Aruba AOS-CX,LAB03,MDF,active
```

**New interfaces** (LAGs on both ends, SVI): [`2-new-interfaces.csv`](csv-examples/2-new-interfaces.csv)
```csv
device,name,type,mode,vlan_group,tagged_vlans,mtu,description
LAB03-F3-ACC-01,lag1,lag,tagged,LAB03 VLANs,"98,120,121",9198,Uplink to AGG-03/04
LAB03-AGG-03,lag5,lag,tagged,LAB03 VLANs,"98,120,121",9198,MC-LAG to LAB03-F3-ACC-01
```
`vlan_group` tells NetBox which group the VLAN IDs belong to; several VLANs go in
quotes, comma separated.

**Existing ports** (they came with the device type): [`3-update-ports.csv`](csv-examples/3-update-ports.csv)
```csv
id,device,name,lag,mode,vlan_group,untagged_vlan,tagged_vlans,description
10751,LAB03-F3-ACC-01,1/1/49,lag1,,,,,To LAB03-AGG-03 1/1/5
10703,LAB03-F3-ACC-01,1/1/1,,tagged,LAB03 VLANs,120,121,USER
```
An import **updates** an object only when the CSV has an `id` column. To get the
IDs: *Devices → Interfaces*, filter by device, **Configure Table** → add the **ID**
column → **Export → Current view**. Open it in Excel, keep `ID`, `Device` and
`Name`, add the columns you want to change, and **rename the header row to the
field names** shown above (`id`, `device`, `name`, `lag`, ...): the export uses
display names, the import expects field names. The IDs in the example file are
from the test system; yours will differ.

**Cables**: [`4-cables.csv`](csv-examples/4-cables.csv)
```csv
side_a_device,side_a_type,side_a_name,side_b_device,side_b_type,side_b_name,type,status
LAB03-F3-ACC-01,dcim.interface,1/1/49,LAB03-AGG-03,dcim.interface,1/1/5,mmf-om4,connected
```

**IP addresses**: [`5-ip-addresses.csv`](csv-examples/5-ip-addresses.csv)
```csv
address,status,role,device,interface,is_primary
10.3.98.105/24,active,,LAB03-F3-ACC-01,vlan98,true
```
`is_primary = true` sets it as the device's primary IP. For a gateway VIP set
`role` to `vip` and import it once for each VSX member.

### Tips

- Start from an **Excel cabling plan** (device A, port A, device B, port B, type):
  that sheet *is* the cables CSV.
- Run **Validate site** after every import step; it points at exactly what is missing.
- Untouched access ports have no VLAN mode and stay `no shutdown` with no VLAN. Best
  practice: give them a profile, or disable them and put them in `Parking`.
- Changes are in **Operations → Changelog**, so you can always see who changed what.
