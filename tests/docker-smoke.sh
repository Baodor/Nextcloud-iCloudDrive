#!/usr/bin/env bash
# Run the built test image with the production network, secrets and hardening.
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
directory="$(mktemp -d)"
project="icloud-docker-smoke-$$"
privilege=()
if [[ "$(id -u)" != 0 ]]; then privilege=(sudo -n); fi
export NEXTCLOUD_URL="https://cloud.example.invalid"
export NEXTCLOUD_NETWORK="$project"
compose=(docker compose --project-directory "$directory" -p "$project" -f "$directory/compose.yaml" -f "$directory/compose.override.yaml")
cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  docker network rm "$NEXTCLOUD_NETWORK" >/dev/null 2>&1 || true
  "${privilege[@]}" rm -rf -- "$directory"
}
trap cleanup EXIT
trap '"${compose[@]}" logs --tail=50 icloud-bridge >&2 || true' ERR
cp "$project_root/compose.yaml" "$directory/compose.yaml"
cat > "$directory/compose.override.yaml" <<YAML
services:
  icloud-bridge:
    image: icloud-bridge:test
    build:
      context: "$project_root/worker"
    labels:
      traefik.enable: "false"
YAML
python3 - "$directory" <<'PY'
import base64
import os
from pathlib import Path
import secrets
import sys
os.umask(0o077)
root = Path(sys.argv[1]) / "secrets"
root.mkdir(mode=0o700)
(root / "api_token").write_text(secrets.token_urlsafe(48) + "\n")
(root / "encryption_key").write_text(base64.urlsafe_b64encode(os.urandom(32)).decode() + "\n")
PY
"${privilege[@]}" chown -R 10001:10001 "$directory/secrets"
docker network create "$NEXTCLOUD_NETWORK" >/dev/null
"${compose[@]}" up -d --no-build --wait --wait-timeout 120
"${compose[@]}" exec -T -w /tmp icloud-bridge python3 - <<'PY'
import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import bridge.server
from cryptography.fernet import Fernet
assert os.getuid() == 10001
assert bridge.server.__file__ == "/app/bridge/server.py"
assert not os.access("/app/bridge/server.py", os.W_OK)
cipher = Fernet(Fernet.generate_key())
assert cipher.decrypt(cipher.encrypt(b"docker-smoke")) == b"docker-smoke"
for name in ("config_password.py", "access-check.txt"):
    assert (Path("/app") / name).read_text()
with urlopen("http://127.0.0.1:8080/health", timeout=5) as response:
    assert json.load(response)["status"] == "ok"
endpoint = "http://127.0.0.1:8080/v1/state"
try:
    urlopen(endpoint, timeout=5)
except HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("Unauthenticated API access succeeded")
request = Request(endpoint, headers={
    "Authorization": "Bearer " + bridge.server.read_secret("BRIDGE_API_TOKEN"),
    "X-Bridge-User": base64.b64encode(b"docker-smoke").decode(),
})
with urlopen(request, timeout=5) as response:
    state = json.load(response)
assert state["nextcloud_user"] == "docker-smoke"
assert state["jobs"] == []
assert state["rclone_version"].startswith("v1.75.1")
assert state["capabilities"]["iwork_download_size"] == 1
assert "RCLONE_ENCRYPT_V0:" in Path("/data/rclone.conf").read_text().splitlines()
print("Production Compose startup, runtime imports and API authentication passed")
PY
