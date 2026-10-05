#!/usr/bin/env bash
set -euo pipefail
version="${NC_VERSION:-30}"
nc="nc-smoke-$version"
bridge="bridge-smoke-$version"
network="icloud-smoke-$version"
token="temporary-integration-token-with-at-least-48-characters"
password="temporary-integration-password"
setup_dir="$(mktemp -d)"
cleanup() { docker rm -f "$nc" "$bridge" >/dev/null 2>&1 || true; docker network rm "$network" >/dev/null 2>&1 || true; rm -rf -- "$setup_dir"; }
trap cleanup EXIT
diagnostics() {
  docker logs --tail=30 "$nc" 2>&1 || true
  docker exec "$nc" tail -c 12000 /var/www/html/data/nextcloud.log 2>/dev/null || true
  docker logs --tail=30 "$bridge" 2>&1 || true
}
trap diagnostics ERR
docker network create "$network"
docker build -t icloud-bridge:test worker
key="$(docker run --rm icloud-bridge:test python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
docker run -d --name "$nc" --network "$network" -e SQLITE_DATABASE=nextcloud \
  -e NEXTCLOUD_ADMIN_USER=admin -e NEXTCLOUD_ADMIN_PASSWORD="$password" \
  -e NEXTCLOUD_TRUSTED_DOMAINS="localhost $nc" "nextcloud:$version-apache"
docker run -d --name "$bridge" --network "$network" -e "NEXTCLOUD_URL=http://$nc" \
  -e "BRIDGE_API_TOKEN=$token" -e "BRIDGE_ENCRYPTION_KEY=$key" icloud-bridge:test
ready=false
for attempt in $(seq 1 120); do
  if docker exec -u www-data "$nc" php occ status --output=json 2>/dev/null | python3 -c 'import json,sys; assert json.load(sys.stdin)["installed"]' 2>/dev/null; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then docker logs "$nc"; exit 1; fi
# Installed config appears before the image's entrypoint starts Apache.
ready=false
for attempt in $(seq 1 120); do
  if docker exec "$nc" curl -fsS http://localhost/status.php 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["installed"] and not d.get("maintenance")' 2>/dev/null; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then docker logs "$nc"; exit 1; fi
ready=false
for attempt in $(seq 1 30); do
  if docker exec "$bridge" python3 -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:8080/health",timeout=2)' 2>/dev/null; then ready=true; break; fi
  sleep 1
done
if [[ "$ready" != true ]]; then docker logs "$bridge"; exit 1; fi
printf '%s\n' "$token" > "$setup_dir/api_token"
chmod 600 "$setup_dir/api_token"
if [[ "$(id -u)" == 0 ]]; then
  chown 10001:10001 "$setup_dir/api_token"
else
  sudo -n chown 10001:10001 "$setup_dir/api_token"
fi
BRIDGE_API_TOKEN_FILE="$setup_dir/api_token" BRIDGE_WORKER_URL="http://$bridge:8080" \
  bash scripts/setup-nextcloud-app.sh "$nc"
docker exec -e "OC_PASS=$password" -u www-data "$nc" php occ user:add --password-from-env member
echo "Checking member OCS state"
# Recent Nextcloud releases briefly retain pre-installation appconfig in APCu.
docker exec "$nc" curl --retry 5 --retry-all-errors --retry-delay 1 -fsS -u "member:$password" -H 'OCS-APIRequest: true' \
  'http://localhost/ocs/v2.php/apps/icloud_drive/api/state?format=json' > /tmp/icloud-smoke-state.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/icloud-smoke-state.json'))['ocs']['data']
assert d['nextcloud_user']=='member',d
assert d['connection']['icloud_connected'] is False,d
PY
echo "Checking rendered member page"
docker exec "$nc" curl -fsS -u "member:$password" \
  'http://localhost/index.php/apps/icloud_drive/' | python3 -c 'import sys; html=sys.stdin.read(); assert "id=\"icloud-bridge\"" in html; assert "data-admin=\"false\"" in html'
# A normal member must not read administrator-only configuration.
code="$(docker exec "$nc" curl -s -o /dev/null -w '%{http_code}' -u "member:$password" -H 'OCS-APIRequest: true' 'http://localhost/ocs/v2.php/apps/icloud_drive/api/admin?format=json')"
[[ "$code" == 403 ]]
echo "Connecting member Nextcloud WebDAV"
docker exec "$nc" curl -fsS -u "member:$password" -H 'OCS-APIRequest: true' -H 'Content-Type: application/json' \
  -d "{\"payload\":{\"username\":\"member\",\"password\":\"$password\"}}" \
  'http://localhost/ocs/v2.php/apps/icloud_drive/api/connect/nextcloud?format=json' > /tmp/icloud-smoke-connect.json
python3 - <<'PY'
import json
assert json.load(open('/tmp/icloud-smoke-connect.json'))['ocs']['data']['connected'] is True
PY
echo "Checking empty-payload stop through Nextcloud OCS"
# Seed a queued fixture without placing it in the runner's in-memory queue.
docker exec -i "$bridge" python3 - <<'PY'
from pathlib import Path
from bridge.server import read_secret
from bridge.store import Store
store = Store(Path('/data'), read_secret('BRIDGE_ENCRYPTION_KEY'))
try:
    store.put('runs', 'member', {
        'id': 'b' * 32, 'uid': 'member', 'job_id': 'a' * 32,
        'job_name': 'Queued cancellation fixture', 'action': 'preview',
        'status': 'queued', 'started': '2026-10-05T10:00:00+00:00',
        'finished': None, 'stats': {}, 'log': '',
    })
finally:
    store.db.close()
PY
docker exec "$nc" curl -fsS -u "member:$password" -H 'OCS-APIRequest: true' -H 'Content-Type: application/json' \
  -d '{"payload":{}}' \
  'http://localhost/ocs/v2.php/apps/icloud_drive/api/runs/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/stop?format=json' \
  | python3 -c 'import json,sys; run=json.load(sys.stdin)["ocs"]["data"]; assert run["status"] == "stopped",run; assert run["finished"],run'
echo "Checking empty-payload disconnect through Nextcloud OCS"
docker exec "$nc" curl -fsS -u "member:$password" -H 'OCS-APIRequest: true' -H 'Content-Type: application/json' \
  -d '{"payload":{}}' \
  'http://localhost/ocs/v2.php/apps/icloud_drive/api/disconnect/nextcloud?format=json' \
  | python3 -c 'import json,sys; assert json.load(sys.stdin)["ocs"]["data"]["disconnected"] == "nextcloud"'
echo "Nextcloud $version integration passed"
