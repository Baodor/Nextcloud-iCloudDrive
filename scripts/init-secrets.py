#!/usr/bin/env python3
"""Create secrets once; never replace existing keys or print their contents."""
import base64
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parents[1]
directory = root / "secrets"
directory.mkdir(mode=0o700, exist_ok=True)
directory.chmod(0o700)
for name, value in (("api_token", secrets.token_urlsafe(48)),
                    ("encryption_key", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())):
    target = directory / name
    if target.exists():
        print(f"Kept existing {target.relative_to(root)}")
        continue
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value + "\n")
    print(f"Created {target.relative_to(root)}")
print("For the non-root container: sudo chown -R 10001:10001 secrets")
