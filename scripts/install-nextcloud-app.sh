#!/usr/bin/env bash
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
app_dir="${NEXTCLOUD_APP_DIR:-}"
if [[ -z "$app_dir" ]]; then
  app_dir="$(docker exec -u "$nc_user" -w "$nc_root" "$container" php -r 'define("OC_CONSOLE", 1); require "lib/base.php"; foreach (\OCP\Server::get(\OCP\IConfig::class)->getSystemValue("apps_paths", []) as $p) { if ($p["writable"] ?? false) { echo $p["path"]; exit; } } exit(1);')"
fi
if [[ "$app_dir" != /* || "$nc_root" != /* ]]; then
  echo "Could not detect absolute Nextcloud paths. Set NEXTCLOUD_ROOT and NEXTCLOUD_APP_DIR." >&2
  exit 1
fi
stamp="$(date -u +%Y%m%d-%H%M%S)"
staging="$app_dir/.icloud-drive-staging-$stamp"
target="$app_dir/icloud_drive"
backup="/tmp/icloud-drive-backup-$stamp"
docker cp "$project_dir/nextcloud/icloud_drive" "$container:$staging"
docker exec "$container" chown -R "$nc_user" "$staging"
docker exec "$container" sh -c 'if [ -e "$1" ]; then mv "$1" "$2"; fi; mv "$3" "$1"' sh "$target" "$backup" "$staging"
if ! docker exec -u "$nc_user" -w "$nc_root" "$container" php occ app:enable icloud_drive; then
  docker exec "$container" sh -c 'mv "$1" "$3"; if [ -e "$2" ]; then mv "$2" "$1"; fi' sh "$target" "$backup" "$staging"
  echo "Enabling failed; the previous app directory was restored. Inspect the Nextcloud log." >&2
  exit 1
fi
echo "Installed icloud_drive into $container:$target"
echo "Open iCloud Drive in Nextcloud, then Administration to set the internal URL and API token."
echo "Previous app files, if any, are at $container:$backup until the container is replaced."
