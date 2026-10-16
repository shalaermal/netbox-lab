# NetBox Lab: nga dizajni te config-et

Lab personal për rrjedhën e plotë të një projekti të ri rrjeti, ashtu siç bëhet në kompani që punojnë me **source of truth**:

```
intent/*.yml  ──►  NetBox  ──►  check  ──►  configs/*.cfg
 (dizajni)        (e vërteta)   (validim)    (Aruba CX)
```

1. **Dizajnin** e shkruan një herë, si rregulla, në pak skedarë YAML (`intent/`).
2. Kodi e nxjerr dizajnin e plotë dhe e **plotëson NetBox-in**: pajisjet, portat, kabllot, LAG-et, VLAN-et, prefix-et, IP-të.
3. Kontrollet **validojnë NetBox-in**: trunk-e të njëjta në të dy anët, uplink të dyfishtë, gateway për çdo VLAN, VSX në rregull...
4. Config-et e switch-ave **gjenerohen nga NetBox**, jo nga YAML-i. Nëse dikush ndryshon diçka në GUI, kjo shfaqet edhe te config-i.

Të gjitha të dhënat janë **fiktive**. Mos fut kurrë IP, emra apo sekrete nga puna.

---

## Projekti shembull: LAB01

| | |
|---|---|
| Topologjia | 2 core (VSX) → 2 agg (VSX) → 44 access, të lidhur me MC-LAG. 2 firewall në HA. |
| Pajisjet | **50**: Aruba 8360-48Y6C (core/agg), Aruba 6300M-48G (access), firewall fiktiv |
| VLAN | **62**: 8 funksione × 6 kate + 12 VLAN për krejt site-in + transit + parking |
| Routing | OSPF area 0 core ↔ agg (/31), gateway-t me VSX active-gateway në agg, default te firewall-i |
| Kabllo / LAG | 102 kabllo, 142 LAG |

### Plani i IP-ve (rregulli: VLAN ID = okteti i 3-të)

| Çka | Ku | Shembull |
|---|---|---|
| VLAN-et e kateve | `vid = 100 + 10×kati + funksioni` | Kati 1 Users = VLAN 110 = `10.1.110.0/24` |
| VLAN-et e site-it | VLAN 20–99 | WiFi-Corp = VLAN 20 = `10.1.20.0/24` |
| Gateway në çdo /24 | `.1` VIP, `.2` AGG-01, `.3` AGG-02 | `10.1.110.1` |
| Firewall transit | VLAN 900, `10.1.250.0/29` | `.1` FW, `.2/.3` core, `.4` VIP e core-it |
| P2P core-agg | `10.1.251.0/24` në /31 | |
| VSX keepalive | `10.1.252.0/24`, VRF KA | |
| OOB mgmt | `10.1.253.0/24`, VRF mgmt | |
| Loopback | `10.1.254.x/32` | router-id |
| Mgmt e access-ave | VLAN 99, `10.1.99.101` e tutje | |

### Best practices që i zbaton kodi
- **VSX** në core dhe agg: ISL `lag 256`, keepalive në port të dedikuar në VRF `KA`, system-mac unik për çdo çift.
- **Access me dy uplink** në MC-LAG, pa porta të bllokuara nga STP.
- **Edge ports**: `admin-edge`, `bpdu-guard`, `loop-protect`. MC-LAG-et e agg-së kanë `root-guard`, dhe agg-u është root bridge.
- **DHCP snooping** në access (trust vetëm në uplink) dhe **DHCP relay** vetëm në VLAN-et e klientëve.
- **Pruning i VLAN-eve**: çdo access merr vetëm VLAN-et e katit të vet plus ato të site-it, jo të 62.
- **Portat që nuk përdoren janë `shutdown`**, dhe portat e lira të access-it janë në VLAN 999 Parking.
- **Menaxhim i ndarë**: core/agg në OOB (VRF mgmt), access in-band në VLAN 99. Pa VLAN 1 si native në trunk.
- **MTU jumbo** në lidhjet infrastrukturore, e vendosur te LAG-u (siç e kërkon AOS-CX).
- **Sekretet nuk janë në git**: config-et kanë `<<TACACS_KEY>>`, `<<SNMP_AUTH_PASS>>` etj., që plotësohen në deploy.
- **Idempotencë dhe "plan" para "apply"**: çdo ndryshim shihet para se të aplikohet, dhe ekzekutimi i dytë nuk ndryshon asgjë.

---

## Si ta nisësh (GitHub Codespaces)

1. Te repo-ja: **Code → Codespaces → Create codespace on main**. Herën e parë merr 5–10 minuta; NetBox-i starton vetë.
2. Te tab-i **PORTS** → porti **8000** → ikona e globit 🌐. Login: **admin / netboxlab**.
3. Në NetBox: **profili → API Tokens → Add a Token** (Write enabled). Vendose token-in te `.env` (`NETBOX_TOKEN=`).
4. Në terminal:

```bash
python -m nblab design     # çka prodhon dizajni (pa NetBox)
python -m nblab plan       # çka do të ndryshojë në NetBox (nuk ndryshon asgjë)
python -m nblab apply      # plotëson NetBox-in (herën e parë rreth 5 minuta)
python -m nblab check      # validon të dhënat në NetBox
python -m nblab render     # gjeneron configs/lab01/*.cfg
```

Komanda të tjera:
```bash
python -m nblab apply --prune   # fshin edhe objektet e lab-it që s'janë më në dizajn
python -m nblab render --device LAB01-AGG-01
python -m nblab reset --yes     # fshin gjithçka me tag-un lab-managed
pytest -q                       # rregullat e dizajnit, pa NetBox
```

Kur mbaron: kutia blu majtas poshtë → **Stop Current Codespace**.

---

## Struktura

```
intent/                 ← VETËM këto i ndryshon me dorë
  site-lab01.yml          site-i, katet, modelet, plani i portave, VSX
  vlan-plan.yml           funksionet e VLAN-eve, VLAN-et e site-it, rangjet infra
  port-profiles.yml       profilet e portave (user, ap, printer...) dhe ku vendosen
  standards.yml           NTP, DNS, syslog, TACACS, OSPF, STP, MTU (config context)
devicetypes/            modelet Aruba nga devicetype-library (portat reale)
nblab/
  design.py               intent → dizajni i plotë (pa NetBox, testohet offline)
  sync.py                 plan / apply drejt NetBox
  snapshot.py             lexon një site nga NetBox
  checks.py               rregullat e validimit
  render.py               NetBox → context → Jinja2
templates/aruba-cx/     template-t sipas seksioneve (system, aaa, vlans, stp, interfaces, vsx, routing)
configs/lab01/          config-et e gjeneruara (në git, që çdo ndryshim të shihet si diff)
tests/                  rregullat e dizajnit, ekzekutohen edhe në GitHub Actions
```

---

## Ushtrime

1. **Lexo një config.** Hap `configs/lab01/LAB01-AGG-01.cfg` dhe gjej: MC-LAG-un e `F1-ACC-01`, gateway-in e VLAN 110 dhe keepalive-in e VSX. Pastaj gjej të njëjtat në NetBox GUI.
2. **Shto një kat.** Shto `{floor: 7, access_switches: 3}` te `site-lab01.yml`. Ekzekuto `plan`: sa pajisje, kabllo dhe VLAN dalin? Pastaj `apply`, `check`, `render`, dhe shiko `git diff configs/`.
3. **Ndrysho një profil.** Bëj portat 41–42 `camera` në vend të `printer`. Shiko që `plan` prek vetëm ato porta.
4. **Gabim në GUI.** Në NetBox, hiq VLAN 113 nga `lag5` në AGG-02. Ekzekuto `check`: duhet të dalin dy gabime (trunk dhe mclag) dhe `render` refuzon. `plan` e tregon devijimin, `apply` e rregullon.
5. **Gabim në intent.** Vendos dy profile në të njëjtat porta, ose shto aq access sa përplasen me portin e keepalive. Shiko që `pytest` (dhe CI-ja në GitHub) e kap para se të prekë NetBox-in.
6. **Shto një rregull të ri** te `checks.py`, p.sh. "çdo port i ndezur në access duhet të ketë përshkrim".
7. **Workflow si në kompani.** Branch → ndryshim në `intent/` → `apply` + `render` → commit edhe të config-eve → Pull Request. Reviewer-i e sheh ndryshimin si diff te `configs/`.

## Hapat e ardhshëm
- **Deploy**: Nornir + Scrapli/NAPALM me `diff` para `commit`, kur të kesh pajisje (p.sh. në EVE-NG).
- **NetBox Config Templates**: template-t ruhen në NetBox si Data Source nga git, dhe config-i shihet direkt te pajisja.
- **802.1X/MAC auth** në porta (port-access) në vend të profileve statike.
- **Monitorim**: Zabbix i lidhur me NetBox me `netbox-zabbix-sync`.
