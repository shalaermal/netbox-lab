#!/usr/bin/env bash
# Runs once, when the Codespace is created.
# Installs Python packages and downloads netbox-docker next to this repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NBD_DIR="/workspaces/netbox-docker"

python3 -m pip install --user -r "$REPO_DIR/requirements.txt"

# Local settings file for the scripts (never committed, see .gitignore)
if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
fi

if [ ! -d "$NBD_DIR" ]; then
  git clone --depth 1 -b release https://github.com/netbox-community/netbox-docker.git "$NBD_DIR"
fi

# Our local changes to netbox-docker:
#  - publish NetBox on port 8000
#  - trust the Codespaces https address (otherwise login fails with a CSRF error)
#  - create the admin user automatically on first start
# Exact public address of port 8000 in this Codespace (a wildcard is not enough)
if [ -n "${CODESPACE_NAME:-}" ]; then
  ORIGIN="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
else
  ORIGIN="https://*.app.github.dev"
fi

cat > "$NBD_DIR/docker-compose.override.yml" <<EOF
services:
  netbox:
    ports:
      - "8000:8080"
    environment:
      CSRF_TRUSTED_ORIGINS: "${ORIGIN} http://localhost:8000"
      SKIP_SUPERUSER: "false"
      SUPERUSER_NAME: "admin"
      SUPERUSER_EMAIL: "admin@lab.local"
      SUPERUSER_PASSWORD: "netboxlab"
EOF

cd "$NBD_DIR"
docker compose pull
echo "Setup done. NetBox will start in the next step (start.sh)."
