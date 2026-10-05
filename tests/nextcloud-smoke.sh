#!/usr/bin/env bash
set -euo pipefail
version="${NC_VERSION:-30}"
nc="nc-smoke-$version"
bridge="bridge-smoke-$version"
network="icloud-smoke-$version"
token="temporary-integration-token-with-at-least-48-characters"
password="temporary-integration-password"
cleanup() { docker rm -f "$nc" "$bridge" >/dev/null 2>&1 || true; docker network rm "$network" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network create "$network"
docker build -t icloud-bridge:test worker
key="$(docker run --rm icloud-bridge:test python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
docker run -d --name "$nc" --network "$network" -e SQLITE_DATABASE=nextcloud \
  -e NEXTCLOUD_ADMIN_USER=admin -e NEXTCLOUD_ADMIN_PASSWORD="$password" \
  -e NEXTCLOUD_TRUSTED_DOMAINS="localhost $nc" \
  -v "$PWD/nextcloud/icloud_drive:/var/www/html/custom_apps/icloud_drive:ro" "nextcloud:$version-apache"
docker run -d --name "$bridge" --network "$network" -e "NEXTCLOUD_URL=http://$nc" \
  -e "BRIDGE_API_TOKEN=$token" -e "BRIDGE_ENCRYPTION_KEY=$key" icloud-bridge:test
ready=false
for attempt in $(seq 1 120); do
  if docker exec -u www-data "$nc" php occ status --output=json 2>/dev/null | python3 -c 'import json,sys; assert json.load(sys.stdin)["installed"]' 2>/dev/null; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then docker logs "$nc"; exit 1; fi
docker exec -u www-data "$nc" php occ app:enable icloud_drive
docker exec -u www-data "$nc" php occ config:app:set icloud_drive worker_url --value="http://$bridge:8080"
docker exec -u www-data "$nc" php occ config:app:set icloud_drive worker_token --value="$token"
docker exec -e "OC_PASS=$password" -u www-data "$nc" php occ user:add --password-from-env member
docker exec "$nc" curl -fsS -u "member:$password" -H 'OCS-APIRequest: true' \
  'http://localhost/ocs/v2.php/apps/icloud_drive/api/state?format=json' > /tmp/icloud-smoke-state.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/icloud-smoke-state.json'))['ocs']['data']
assert d['nextcloud_user']=='member',d
assert d['connection']['icloud_connected'] is False,d
PY
docker exec "$nc" curl -fsS -u "member:$password" -H 'OCS-APIRequest: true' -H 'Content-Type: application/json' \
  -d "{\"payload\":{\"username\":\"member\",\"password\":\"$password\"}}" \
  'http://localhost/ocs/v2.php/apps/icloud_drive/api/connect/nextcloud?format=json' > /tmp/icloud-smoke-connect.json
python3 - <<'PY'
import json
assert json.load(open('/tmp/icloud-smoke-connect.json'))['ocs']['data']['connected'] is True
PY
echo "Nextcloud $version integration passed"
