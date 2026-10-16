#!/usr/bin/env bash
# Runs every time the Codespace starts. Starts NetBox and waits until it answers.
set -uo pipefail

NBD_DIR="/workspaces/netbox-docker"

if [ ! -d "$NBD_DIR" ]; then
  echo "netbox-docker not found. Run: bash .devcontainer/setup.sh"
  exit 0
fi

cd "$NBD_DIR"

# Wait for the Docker daemon (docker-in-docker can take a few seconds)
for _ in $(seq 1 30); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done

docker compose up -d

echo "Waiting for NetBox on http://localhost:8000 (first start takes 2-5 minutes) ..."
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/login/ || true)
  if [ "$code" = "200" ]; then
    echo "NetBox is up. Open the PORTS tab -> port 8000 -> globe icon."
    echo "Login: admin / netboxlab"
    exit 0
  fi
  sleep 5
done

echo "NetBox is still starting. Check with: cd $NBD_DIR && docker compose ps"
exit 0
