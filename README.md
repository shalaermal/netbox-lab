# NetBox Lab

Lab personal për të praktikuar NetBox, pynetbox, git dhe automatizim, krejt në browser me GitHub Codespaces. Të gjitha të dhënat janë **fiktive** (LAB01, LAB02, `10.x.x.x`). Mos fut kurrë IP, emra pajisjesh apo token-a nga puna.

## Si ta nisësh

1. Te kjo repo: **Code → Codespaces → Create codespace on main**.
2. Prit 5–10 minuta herën e parë. Codespace-i e shkarkon dhe e starton NetBox-in vetë.
3. Poshtë, te tab-i **PORTS**, te porti **8000** kliko ikonën e globit 🌐.
4. Hyr me **admin / netboxlab**.
5. Në NetBox: **profili (lart djathtas) → API Tokens → Add a Token**, me *Write enabled*. Kopjoje token-in.
6. Hape skedarin `.env` (krijohet vetë) dhe vendose token-in te `NETBOX_TOKEN=`.
7. Në terminal:
   ```bash
   python scripts/build_lab.py
   ```
   Rifresko NetBox-in. Duhet të shohësh 2 site, 12 VLAN, 12 prefix dhe 8 pajisje me IP menaxhimi.

Herët e tjera, Codespace-i e starton NetBox-in vetë. Nëse nuk hapet, në terminal:
```bash
cd /workspaces/netbox-docker && docker compose up -d
```

**Kur mbaron:** kutia blu majtas poshtë → **Stop Current Codespace**. Te GitHub **Settings → Codespaces** vendos *Default idle timeout* në 30 minuta.

## Struktura

```
data/
  sites.yml            site-et dhe numri i tyre (10.<numri>.x.x)
  standard_vlans.yml   VLAN-et që krijohen në çdo site
  catalog.yml          rolet IPAM, prodhuesit, device types, rolet e pajisjeve
  devices.yml          pajisjet dhe IP-ja e menaxhimit
scripts/
  build_lab.py         e ndërton krejt lab-in nga YAML (mund ta ekzekutosh sa herë të duash)
  check_quality.py     raport për gabimet në të dhëna (pa IP, pa VLAN, pa DNS...)
  reset_lab.py         fshin gjithçka që kanë krijuar script-at (tag "lab-managed")
  validate_data.py     kontrollon YAML-at pa NetBox (ekzekutohet edhe në GitHub Actions)
  lablib.py            lidhja me NetBox dhe funksione të përbashkëta
.devcontainer/         konfigurimi i Codespace-it (NetBox me Docker)
.github/workflows/     kontrolli automatik në çdo push
```

## Si funksionon

- **YAML = çka do** (intent). Script-i e lexon dhe e krijon në NetBox.
- Prefix-et llogariten vetë: site 1, VLAN 115 → `10.1.115.0/24`.
- IP-ja e menaxhimit: site 1, `mgmt_host: 11` → `10.1.99.11/24`.
- `build_lab.py` është **idempotent**: nëse objekti ekziston e përditëson, nëse jo e krijon. Mund ta ekzekutosh 10 herë dhe rezultati është i njëjti.
- Çdo objekt merr tag-un `lab-managed`, kështu që `reset_lab.py` fshin vetëm ato, jo gjërat që i krijon me dorë.

## Ushtrime

1. **Njihu me modelin.** Hape një pajisje në GUI dhe gjej site-in, rolin, interface-in `mgmt0`, IP-në primare dhe VLAN-in e prefix-it.
2. **Shto një site.** Shto `LAB03` me `number: 3` te `sites.yml` dhe dy pajisje te `devices.yml`. Ekzekuto `validate_data.py`, pastaj `build_lab.py`.
3. **Shto një VLAN standard.** Shto `{vid: 30, name: Cameras, role: iot}` te `standard_vlans.yml`. Ekzekuto `build_lab.py` dhe shiko që u krijua në të gjitha site-et.
4. **Thyeje me qëllim.** Në GUI fshij IP-në e një pajisjeje dhe hiq VLAN-in nga një prefix. Ekzekuto `check_quality.py` dhe shiko a i kap. Pastaj `build_lab.py` i rregullon.
5. **Fshij krejt dhe rindërto.** `python scripts/reset_lab.py --yes`, pastaj `build_lab.py`. Ky është përfitimi i madh: NetBox-i mund të rindërtohet nga git.
6. **Git workflow.** Krijo branch `add-lab03`, bëj ndryshimin, `git push` dhe hap Pull Request. GitHub Actions ekzekuton `validate_data.py`. Provo me qëllim një gabim (p.sh. dy pajisje me të njëjtin emër) dhe shiko që PR-i del i kuq.
7. **Script i yti.** Shkruaj `scripts/export_csv.py` që i nxjerr të gjitha pajisjet me IP në CSV.
8. **Më vonë:** shto Zabbix në Codespace dhe lidhe me `netbox-zabbix-sync`. Custom fields `zabbix_hostid` dhe `zabbix_template` i krijon tashmë `build_lab.py`.

## Komanda të dobishme

```bash
python scripts/validate_data.py          # kontrollo YAML-at
python scripts/build_lab.py              # ndërto ose përditëso lab-in
python scripts/check_quality.py          # raporti i cilësisë
python scripts/check_quality.py --strict # kthen gabim nëse ka probleme
python scripts/reset_lab.py --yes        # fshij objektet e lab-it

cd /workspaces/netbox-docker
docker compose ps                        # statusi i NetBox-it
docker compose logs -f netbox            # log-et
```
