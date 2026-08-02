---
paths:
  - "email-extractor/config.yaml"
  - "email-extractor/app/__init__.py"
  - "email-extractor/Dockerfile"
---

# Deploying the live HA add-on (`e0ac7775_email_extractor`)

Host/SSH/dashboard credentials live in **local memory only** (`ha-server-access.md`,
`extractor-addon-supervisor.md`, `email-extractor-deploy.md`) — never commit them here.
This file has the PROCEDURE; memory has the VALUES.

## It is a real supervisor add-on, not a raw container

Installed from this repo as a HA Supervisor store add-on (slug `e0ac7775_email_extractor`,
container `app_e0ac7775_email_extractor` since 2026-07-30's `addon_` → `app_` rename).
Config lives in `/data/options.json` INSIDE the container, not in git.

## Deploy = one command, after main CI has built + pushed the GHCR image

`build` in `.github/workflows/ci.yml` only pushes `ghcr.io/zbynekdrlik/email-extractor-amd64:<version>`
on a `main` push (reads the version from `config.yaml`) — wait for that job green first.

```bash
ssh <ha-user>@<ha-host>          # values: memory ha-server-access.md; sshpass -p "$PW" if no key
export SUPERVISOR_TOKEN=$(cat /run/s6/container_environment/HASSIO_TOKEN)   # required — `ha` else says "unauthorized"
ha store reload                                          # Supervisor's store-metadata cache does NOT
ha addons info e0ac7775_email_extractor --raw-json \      # auto-refresh right after a CI build; without
  | jq '.data | {version, version_latest, update_available}'   # this, `ha addons update` can say
ha addons update e0ac7775_email_extractor                 # "No update available" even though the
ha addons info e0ac7775_email_extractor --raw-json \      # image already exists in GHCR
  | jq '.data | {version, state}'                          # confirm the new version + state:"started"
ha addons logs e0ac7775_email_extractor | tail -20
```

(`ha addons ...` is deprecated in favour of `ha apps ...` — still works, just prints a
warning; both alias the same command.)

## Post-deploy verification

- Liveness: `curl http://<ha-host>:8099/health` → `{"ok":true,"version":"<x.y.z>"}`.
- Version-on-DOM: any page (`/otazky`, the main dashboard) shows `v<x.y.z>` in the header —
  read it with Playwright, not curl.
- Functional: `/otazky` lists live open warehouse questions — a real, current cross-check
  for whatever the ticket changed in the matching ladder (`app/orders/match.py`).

## Known gotchas

- **`scp` to this box fails** ("subsystem request failed", SFTP subsystem likely
  disabled). Base64-encode locally, pipe through `ssh ... "echo '<b64>' | base64 -d >
  /tmp/x"`, then `docker cp` from the HOST into the container if the file needs to land
  inside it.
- **No `sqlite3` CLI inside the n8n add-on container** (only relevant if you're cross-
  checking n8n's own DB) — use node's bundled sqlite3 module instead; `docker exec`
  output truncates around 64 KB, so large dumps need chunking/base64.
