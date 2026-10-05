"""Preserve iWork package contents and original versions before ordinary sync."""
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import hashlib
import tempfile
import time
import uuid
import zipfile
from .models import BACKUPS, BridgeError, tenant
from .nextcloud_dav import NextcloudDAV


SUFFIXES = (".pages", ".numbers", ".key")


def glob_literal(value):
    return "".join("\\" + c if c in "\\*?[]{}" else c for c in value)


class CheckedReader:
    def __init__(self, file, check):
        self.file, self.check = file, check

    def read(self, size=-1):
        self.check()
        return self.file.read(size)


class IWork:
    def __init__(self, engine, uid, job, run, refs, deadline=None):
        self.engine, self.uid, self.job, self.run, self.refs = engine, uid, job, run, refs
        self.deadline = deadline or time.monotonic() + job["timeout_minutes"] * 60
        self.documents = []
        self.next_update = time.monotonic() + 2

    def check(self):
        current = self.engine.store.get("runs", self.uid, self.run["id"])
        if not current or current.get("cancel_requested") or self.engine.stop_event.is_set() or time.monotonic() > self.deadline:
            self.run["cancel_requested"] = True
            raise BridgeError("Run stopped while preparing complete iWork documents.", 409)
        if self.run.get("progress", {}).get("phase") == "iwork" and time.monotonic() >= self.next_update:
            self.run["progress"]["updated"] = datetime.now(timezone.utc).isoformat()
            with self.engine.lock:
                if self.engine.store.get("runs", self.uid, self.run["id"]):
                    self.engine.store.put("runs", self.uid, self.run)
            self.next_update = time.monotonic() + 2

    def publish(self):
        self.check()
        self.run["progress"] = {"phase": "iwork", "percent": None, "updated": datetime.now(timezone.utc).isoformat()}
        self.next_update = time.monotonic() + 2
        with self.engine.lock:
            if self.engine.store.get("runs", self.uid, self.run["id"]):
                self.engine.store.put("runs", self.uid, self.run)

    def plan(self):
        if not self.job.get("iwork_packages", True):
            return []
        self.publish()
        result = self.engine.rc.call("operations/list", {"fs": self.refs["nextcloud"], "remote": "",
            "opt": {"recurse": True, "dirsOnly": True}, "_filter": {"ExcludeRule": self.job["excludes"]}})
        for item in sorted(result.get("list", []), key=lambda x: (x["Path"].count("/"), x["Path"])):
            self.check()
            relative = item["Path"]
            if not item.get("IsDir") or not relative.lower().endswith(SUFFIXES):
                continue
            if any(relative.startswith(parent + "/") for parent in self.documents):
                continue
            other = self.engine.rc.call("operations/stat", {"fs": self.refs["icloud"], "remote": relative}).get("item")
            # Both sides already expose directories: rclone can copy them normally.
            if other and other.get("IsDir"):
                continue
            self.documents.append(relative)
        if self.documents:
            self.run["iwork"] = {"planned": len(self.documents), "completed": 0,
                "documents": self.documents[:100], "preview": self.run["action"] == "preview",
                "backup_root": f"{BACKUPS}/iWork/{self.job['id']}/{self.run['id']}"}
            self.publish()
        return self.documents

    def preview_filters(self):
        # Normal files are still dry-run by rclone. Packages have a separate plan;
        # do not simulate hundreds of child uploads into an existing iCloud file.
        return [pattern for path in self.documents for pattern in
                ("/" + glob_literal(path), "/" + glob_literal(path) + "/**")]

    def normalize(self):
        if not self.documents or self.run["action"] == "preview":
            return
        self.check()
        client = NextcloudDAV.for_user(self.engine, self.uid)
        scratch = self.engine.root / "iwork" / tenant(self.uid)
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            archive = Path(temporary) / "document.zip"
            for relative in self.documents:
                self.check()
                original = self.job["nextcloud_path"] + "/" + relative
                source = client.stat(original)
                if not source or not source["directory"] or not source["etag"]:
                    raise BridgeError("An iWork package changed before it could be copied. Retry this run.", 409)
                if source["etag"].startswith("W/"):
                    raise BridgeError("Nextcloud cannot safely check the current iWork package version.", 409)
                self.run["iwork"]["current"] = relative
                self.publish()
                entries = self.engine.rc.call("operations/list", {"fs": self.refs["nextcloud"], "remote": relative,
                    "opt": {"recurse": True}}).get("list", [])
                self.pack(client, original, entries, archive, relative)
                self.check()
                backup = self.run["iwork"]["backup_root"] + "/" + relative
                staged = self.run["iwork"]["backup_root"] + "/incoming/" + uuid.uuid4().hex + PurePosixPath(relative).suffix
                client.mkdir_parents(staged)
                client.mkdir_parents(backup)
                digest = hashlib.sha256()
                with archive.open("rb") as file:
                    for chunk in iter(lambda: file.read(1048576), b""):
                        self.check(); digest.update(chunk)
                headers = {"Content-Type": "application/octet-stream", "Content-Length": str(archive.stat().st_size), "If-None-Match": "*"}
                if source["mtime"] is not None:
                    headers["X-OC-Mtime"] = str(source["mtime"])
                with archive.open("rb") as file, client.request("PUT", staged, CheckedReader(file, self.check), headers):
                    pass
                staged_source = client.stat(staged)
                if not staged_source or staged_source["directory"] or not staged_source["etag"]:
                    raise BridgeError("The temporary iWork document is unavailable. The original is unchanged.", 409)
                uploaded = hashlib.sha256()
                with client.request("GET", staged) as response:
                    for chunk in iter(lambda: response.read(1048576), b""):
                        self.check(); uploaded.update(chunk)
                if uploaded.digest() != digest.digest():
                    raise BridgeError("The uploaded iWork document failed integrity verification. The original is unchanged.", 502)
                if (client.stat(original) or {}).get("etag") != source["etag"]:
                    raise BridgeError("An iWork package changed during copying. The original is unchanged; retry this run.", 409)
                with self.engine.lock:
                    self.check()
                    self.run["iwork"]["pending"] = {"original": original, "backup": backup, "staged": staged}
                    self.engine.store.put("runs", self.uid, self.run)
                    try:
                        client.move(original, backup, source["etag"], directory=True)
                        if (client.stat(backup) or {}).get("etag") != source["etag"]:
                            raise BridgeError("The iWork package changed before backup. Its current version will be restored; retry this run.", 409)
                        client.move(staged, original, staged_source["etag"])
                    except Exception:
                        recover(self.engine, self.uid, self.run, client)
                        raise
                    self.run["iwork"].pop("pending", None)
                    self.run["iwork"]["completed"] += 1
                    self.engine.store.put("runs", self.uid, self.run)
                self.publish()
        self.run["iwork"].pop("current", None)
        self.engine.rc.call("fscache/clear")

    def pack(self, client, original, entries, archive, relative):
        expected = []
        seen = set()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as package:
            for entry in sorted(entries, key=lambda x: x["Path"]):
                self.check()
                name = entry["Path"]
                # lsjson returns paths relative to the Fs root, including remote.
                if not name.startswith(relative + "/"):
                    raise BridgeError("Invalid path inside an iWork package.")
                name = name[len(relative) + 1:]
                if name.startswith("/") or any(p in {"", ".", ".."} for p in name.split("/")):
                    raise BridgeError("Invalid path inside an iWork package.")
                if name in seen:
                    raise BridgeError("Duplicate path inside an iWork package.")
                seen.add(name)
                info = zipfile.ZipInfo(name + ("/" if entry.get("IsDir") else ""))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.file_size = max(0, entry.get("Size", 0))
                info.external_attr = ((0o40755 if entry.get("IsDir") else 0o100644) << 16)
                try:
                    modified = datetime.fromisoformat(entry.get("ModTime", "").replace("Z", "+00:00"))
                    if 1980 <= modified.year <= 2107:
                        info.date_time = modified.timetuple()[:6]
                except ValueError:
                    pass
                if entry.get("IsDir"):
                    package.writestr(info, b"")
                    continue
                expected.append(name)
                received = 0
                with client.request("GET", original + "/" + name) as response, package.open(info, "w", force_zip64=info.file_size > zipfile.ZIP64_LIMIT) as output:
                    for chunk in iter(lambda: response.read(1048576), b""):
                        self.check(); output.write(chunk); received += len(chunk)
                if received != entry.get("Size"):
                    raise BridgeError("An iWork package file changed during copying. The original is unchanged.", 409)
        if not expected:
            raise BridgeError("An empty iWork package cannot be copied as a document.", 409)
        try:
            with zipfile.ZipFile(archive) as package:
                if sorted(n for n in package.namelist() if not n.endswith("/")) != sorted(expected):
                    raise zipfile.BadZipFile("Document contents do not match")
                for name in expected:
                    with package.open(name) as file:
                        for _ in iter(lambda: file.read(1048576), b""):
                            self.check()
        except zipfile.BadZipFile:
            raise BridgeError("The complete iWork document failed integrity verification.", 502) from None


def recover(engine, uid, run, client=None):
    """Complete or roll back a rename interrupted by an error or worker restart."""
    pending = run.get("iwork", {}).get("pending")
    if not pending:
        return
    client = client or NextcloudDAV.for_user(engine, uid)
    original, backup = client.stat(pending["original"]), client.stat(pending["backup"])
    if original is None:
        if not backup or not backup["directory"]:
            raise BridgeError("iWork recovery needs review; inspect " + run["iwork"]["backup_root"], 409)
        client.move(pending["backup"], pending["original"], backup["etag"], directory=True)
    elif not original["directory"] and not backup:
        raise BridgeError("iWork recovery needs review; the saved original package could not be found.", 409)
    elif not original["directory"]:
        run["iwork"]["completed"] = min(run["iwork"].get("planned", 1), run["iwork"].get("completed", 0) + 1)
    run["iwork"].pop("pending", None)
    engine.store.put("runs", uid, run)
