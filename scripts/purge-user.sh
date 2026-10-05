#!/usr/bin/env bash
set -euo pipefail
uid="${1:?Usage: bash scripts/purge-user.sh NEXTCLOUD_USER_ID}"
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
docker compose exec -T -e "PURGE_USER_ID=$uid" icloud-bridge python3 - <<'PY'
import base64, json, os, urllib.request
from bridge.server import read_secret
request = urllib.request.Request('http://127.0.0.1:8080/v1/account', method='DELETE', headers={
  'Authorization': 'Bearer ' + read_secret('BRIDGE_API_TOKEN'), 'X-Bridge-User': base64.b64encode(os.environ['PURGE_USER_ID'].encode()).decode()})
with urllib.request.urlopen(request, timeout=120) as response:
    assert json.load(response)['removed']
print('Bridge account removed. Remote files and backup folders were not deleted.')
PY
