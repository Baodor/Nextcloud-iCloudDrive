#!/usr/bin/env python3
"""Build a correctly rooted Nextcloud archive without credentials or runtime data."""
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[1]
output = root / "dist" / "icloud_drive-0.1.0.tar.gz"
output.parent.mkdir(exist_ok=True)
with tarfile.open(output, "w:gz") as archive:
    archive.add(root / "nextcloud" / "icloud_drive", arcname="icloud_drive")
print(output)
