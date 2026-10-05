#!/usr/bin/env bash
# Verify the deployed worker and enable complete document copies for one job.
set -euo pipefail
if (( $# < 1 || $# > 2 )) || [[ ! "$1" =~ ^[a-f0-9]{32}$ ]]; then
  echo "Usage: bash scripts/enable-iwork.sh JOB_ID [NEXTCLOUD_CONTAINER]" >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
expected="$(sha256sum worker/bridge/iwork.py | cut -d ' ' -f 1)"
if [[ -n "${BRIDGE_CONTAINER:-}" ]]; then
  runner=(docker exec -i)
  target="$BRIDGE_CONTAINER"
else
  runner=(docker compose exec -T)
  target="${BRIDGE_SERVICE:-icloud-bridge}"
fi
"${runner[@]}" -e "BRIDGE_JOB_ID=$1" -e "BRIDGE_IWORK_SOURCE_SHA256=$expected" \
  "$target" python3 - <<'PY'
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import os
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

try:
    from bridge import iwork
    from bridge.server import read_secret
except ImportError:
    sys.exit("The running worker does not contain iWork handling. Rebuild and recreate icloud-bridge first.")
if hashlib.sha256(Path(iwork.__file__).read_bytes()).hexdigest() != os.environ['BRIDGE_IWORK_SOURCE_SHA256']:
    sys.exit("The running worker differs from this checkout. Rebuild and recreate icloud-bridge first.")
database = sqlite3.connect('file:/data/bridge.sqlite3?mode=ro', uri=True)
try:
    row = database.execute('SELECT uid,data FROM jobs WHERE id=?', (os.environ['BRIDGE_JOB_ID'],)).fetchone()
finally:
    database.close()
if not row:
    sys.exit("Job not found. Use the existing job ID; do not recreate its folder mapping.")
uid, raw = row
job = json.loads(raw)
opener = build_opener(ProxyHandler({}))
headers = {'Authorization': 'Bearer ' + read_secret('BRIDGE_API_TOKEN'),
           'X-Bridge-User': base64.b64encode(uid.encode()).decode(), 'Content-Type': 'application/json'}
def api(route, data=None):
    request = Request('http://127.0.0.1:8080/v1/' + route, headers=headers,
                      data=json.dumps(data).encode() if data is not None else None,
                      method='PUT' if data is not None else 'GET')
    try:
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.loads(error.read(10000)).get('error', 'Bridge request failed.')
        except (ValueError, UnicodeDecodeError):
            message = 'Bridge request failed.'
        sys.exit(str(message))
    except (URLError, TimeoutError):
        sys.exit('The bridge API is unavailable. Check container health and try again.')
state = api('state')
if state.get('capabilities', {}).get('iwork_packages', 0) < 2:
    sys.exit('The running API has older iWork handling. Recreate the bridge container first.')
old = dict(job)
job['iwork_packages'] = True
workarounds = {'*.pages', '*.pages/**', '*.numbers', '*.numbers/**', '*.key', '*.key/**'}
job['excludes'] = [rule for rule in job['excludes'] if rule not in workarounds]
removed = len(old['excludes']) - len(job['excludes'])
if job != old:
    job = api('jobs/' + job['id'], job)
print('Running worker verified against this checkout (iWork revision 2).')
print('Job: ' + job['name'])
print('Automatic complete Pages/Numbers/Keynote copies: enabled.')
if removed:
    print(str(removed) + ' former iWork workaround exclusions removed; other filters retained.')
print('Next: run Preview, review it, then Initialize this two-way job if required.')
PY
if [[ -n "${2:-}" ]]; then
  docker exec -i -u "${NEXTCLOUD_USER:-www-data}" -w "${NEXTCLOUD_ROOT:-/var/www/html}" "$2" php <<'PHP'
<?php
define('OC_CONSOLE', 1);
require 'lib/base.php';
try {
    $config = \OCP\Server::get(\OCP\IConfig::class);
    $url = rtrim($config->getAppValue('icloud_drive', 'worker_url', 'http://icloud-bridge:8080'), '/');
    $clients = \OCP\Server::get(\OCP\Http\Client\IClientService::class);
    $response = $clients->newClient()->get($url . '/health', [
        'timeout' => 15, 'nextcloud' => ['allow_local_address' => true],
    ]);
    $health = json_decode((string)$response->getBody(), true, 512, JSON_THROW_ON_ERROR);
    if (($health['status'] ?? '') !== 'ok' || ($health['capabilities']['iwork_packages'] ?? 0) < 2) {
        throw new \RuntimeException('Nextcloud is connected to an older or unavailable bridge. Check the configured worker URL and duplicate Docker network aliases.');
    }
    echo "Nextcloud reaches the updated iWork handler through its configured bridge URL.\n";
} catch (\Throwable $error) {
    fwrite(fopen('php://stderr', 'w'), "Nextcloud bridge verification failed: " . $error->getMessage() . "\n");
    exit(1);
}
PHP
fi
