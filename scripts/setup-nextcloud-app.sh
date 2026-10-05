#!/usr/bin/env bash
# Install the app and configure its encrypted service token without displaying it.
set -euo pipefail
container="${1:-nextcloud}"
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
nc_root="${NEXTCLOUD_ROOT:-}"
if [[ -z "$nc_root" ]]; then
  nc_root="$(docker exec "$container" sh -c 'for p in /var/www/html /app/www/public /config/www/nextcloud; do if [ -f "$p/occ" ]; then echo "$p"; exit 0; fi; done; exit 1')"
fi
nc_user="${NEXTCLOUD_USER:-}"
if [[ -z "$nc_user" ]]; then
  nc_user="$(docker exec "$container" sh -c 'if id abc >/dev/null 2>&1; then id -u abc; elif id www-data >/dev/null 2>&1; then id -u www-data; else exit 1; fi')"
fi
export NEXTCLOUD_ROOT="$nc_root" NEXTCLOUD_USER="$nc_user"
bash "$project_dir/scripts/install-nextcloud-app.sh" "$container"
docker exec -u "$nc_user" -w "$nc_root" "$container" php occ app:enable files_external
token_file="${BRIDGE_API_TOKEN_FILE:-$project_dir/secrets/api_token}"
worker_url="${BRIDGE_WORKER_URL:-http://icloud-bridge:8080}"
reader=(cat)
if [[ ! -r "$token_file" ]]; then reader=(sudo cat); fi
"${reader[@]}" -- "$token_file" | docker exec -i -u "$nc_user" -w "$nc_root" \
  -e "ICLOUD_BRIDGE_URL=$worker_url" "$container" php -r '
define("OC_CONSOLE", 1);
require "lib/base.php";
$token = trim(stream_get_contents(STDIN));
if (strlen($token) < 32) {
    fwrite(STDERR, "Invalid or unreadable bridge API token.\n");
    exit(1);
}
$url = getenv("ICLOUD_BRIDGE_URL");
$parts = parse_url($url);
if (!$parts || !in_array($parts["scheme"] ?? "", ["http", "https"], true)
    || empty($parts["host"]) || isset($parts["user"]) || isset($parts["pass"])
    || isset($parts["query"]) || isset($parts["fragment"])) {
    fwrite(STDERR, "Invalid bridge URL.\n");
    exit(1);
}
$config = \OCP\Server::get(\OCP\IConfig::class);
$crypto = \OCP\Server::get(\OCP\Security\ICrypto::class);
$encrypted = $crypto->encrypt($token);
$config->setAppValue("icloud_drive", "worker_url", rtrim($url, "/"));
$config->setAppValue("icloud_drive", "worker_token", "encrypted:" . $encrypted);
echo "Bridge connection configured; API token stored encrypted.\n";
'
