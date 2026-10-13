#!/usr/bin/env bash
# Read-only runtime, job and optional Nextcloud routing diagnostics.
set -euo pipefail
if (( $# > 1 )); then
  echo "Usage: bash scripts/check-runtime.sh [NEXTCLOUD_CONTAINER]" >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
expected="$(python3 - <<'PY'
import hashlib, json
from pathlib import Path
print(json.dumps({name: hashlib.sha256(Path('worker/bridge', name).read_bytes()).hexdigest()
                  for name in ('engine.py', 'progress.py', 'iwork.py')}))
PY
)"
if [[ -n "${BRIDGE_CONTAINER:-}" ]]; then
  runner=(docker exec -i)
  target="$BRIDGE_CONTAINER"
else
  runner=(docker compose exec -T)
  target="${BRIDGE_SERVICE:-icloud-bridge}"
fi
"${runner[@]}" -e "BRIDGE_EXPECTED_SOURCES=$expected" "$target" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.request import build_opener, ProxyHandler

opener = build_opener(ProxyHandler({}))
try:
    with opener.open('http://127.0.0.1:8080/health', timeout=10) as response:
        health = json.load(response)
    version = subprocess.run(['rclone', 'version'], capture_output=True, text=True, check=True, timeout=10).stdout.splitlines()[0]
except Exception:
    sys.exit('Runtime check failed: the bridge API or rclone is unavailable.')
expected = json.loads(os.environ['BRIDGE_EXPECTED_SOURCES'])
source_matches = {name: Path('/app/bridge', name).is_file() and
                  hashlib.sha256(Path('/app/bridge', name).read_bytes()).hexdigest() == digest
                  for name, digest in expected.items()}
report = {'rclone_version': version, 'health': health, 'source_matches_checkout': source_matches, 'jobs': []}
database = sqlite3.connect('file:/data/bridge.sqlite3?mode=ro', uri=True)
try:
    jobs = [json.loads(row[0]) for row in database.execute('SELECT data FROM jobs')]
    runs = [json.loads(row[0]) for row in database.execute('SELECT data FROM runs')]
finally:
    database.close()
for job in jobs:
    history = sorted((run for run in runs if run['job_id'] == job['id']), key=lambda run: run['started'], reverse=True)
    entry = {key: job.get(key) for key in ('id', 'name', 'mode', 'initial', 'initialized', 'enabled', 'iwork_packages')}
    if history:
        latest = history[0]
        entry['last_run'] = {'status': latest['status'], 'action': latest['action'], 'started': latest['started'],
            'rclone_version': latest.get('runtime', {}).get('rclone_version', 'not recorded'),
            'processed_bytes': latest.get('stats', {}).get('bytes', 0),
            ('planned_copies' if latest['action'] == 'preview' else 'completed_copies'): latest.get('stats', {}).get('transfers', 0),
            'failed_files': []}
        for line in latest.get('log', '').splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('level') in ('error', 'fatal') and record.get('object'):
                name = str(record['object'])[:2000]
                if name not in entry['last_run']['failed_files']:
                    entry['last_run']['failed_files'].append(name)
                if len(entry['last_run']['failed_files']) >= 32:
                    break
    report['jobs'].append(entry)
print(json.dumps(report, ensure_ascii=False, indent=2))
capabilities = health.get('capabilities', {})
if not all(source_matches.values()) or capabilities.get('iwork_download_size', 0) < 1 or capabilities.get('icloud_download_errors', 0) < 1:
    sys.exit('The running worker is older than this checkout. Rebuild and recreate icloud-bridge.')
PY
if [[ -n "${1:-}" ]]; then
  nc_root="${NEXTCLOUD_ROOT:-}"
  if [[ -z "$nc_root" ]]; then
    nc_root="$(docker exec "$1" sh -c 'for p in /var/www/html /app/www/public /config/www/nextcloud; do if [ -f "$p/occ" ]; then echo "$p"; exit 0; fi; done; exit 1')"
  fi
  nc_user="${NEXTCLOUD_USER:-}"
  if [[ -z "$nc_user" ]]; then
    nc_user="$(docker exec "$1" sh -c 'if id abc >/dev/null 2>&1; then id -u abc; elif id www-data >/dev/null 2>&1; then id -u www-data; else exit 1; fi')"
  fi
  expected_ui="$(sha256sum nextcloud/icloud_drive/js/app.js | cut -d ' ' -f 1)"
  docker exec -i -e "BRIDGE_EXPECTED_UI=$expected_ui" -u "$nc_user" -w "$nc_root" "$1" php <<'PHP'
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
    $app = \OCP\Server::get(\OCP\App\IAppManager::class)->getAppPath('icloud_drive');
    $uiMatches = hash_file('sha256', $app . '/js/app.js') === getenv('BRIDGE_EXPECTED_UI');
    echo json_encode(['nextcloud_bridge_health' => $health, 'nextcloud_ui_matches_checkout' => $uiMatches], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE) . "\n";
    if (($health['status'] ?? '') !== 'ok' || ($health['capabilities']['icloud_download_errors'] ?? 0) < 1 || !$uiMatches) {
        throw new \RuntimeException('Nextcloud reaches an older bridge or has older app files. Verify its worker URL and reinstall the app files.');
    }
} catch (\Throwable $error) {
    fwrite(fopen('php://stderr', 'w'), "Nextcloud runtime check failed: " . $error->getMessage() . "\n");
    exit(1);
}
PHP
fi
