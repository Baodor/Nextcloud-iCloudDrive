"""Measured progress for rclone's changing queues and multi-stage bisync runs."""
from datetime import datetime, timezone
import json
import math
import re


COUNTERS = ("bytes", "totalBytes", "checks", "totalChecks", "listed", "transfers", "totalTransfers",
            "errors", "deletes", "deletedDirs", "renames", "speed", "elapsedTime")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def classify_failure(message, name="", object_type="", sanitize=str):
    name = sanitize(str(name))[:2000]
    if "is a file not a directory" in message:
        package = re.match(r"^(.*?\.(?:pages|numbers|key))(?:/|$)", name, re.IGNORECASE)
        return {"code": "file_directory_conflict", "path": package[1] if package else name,
                "iwork_package": bool(package)}
    if "502 Bad Gateway" in message and any(text in message for text in
            ("simple update failed", "uploading chunk failed")):
        return {"code": "nextcloud_upload_failed", "path": name, "http_status": 502}
    if any(text in message for text in ("RootURL not set", "iCloud did not supply a usable document download URL",
            "iCloud did not supply a document download token", "cannot resolve complete iWork document")) or (
            "failed to open source object" in message and "iclouddrive" in object_type):
        if not name:
            match = re.search(r'cannot resolve complete iWork document ("(?:[^"\\]|\\.)*"):', message)
            if match:
                try:
                    name = sanitize(json.loads(match[1]))[:2000]
                except ValueError:
                    pass
        return {"code": "icloud_download_failed", "path": name}
    return None


class Progress:
    """Only a completed checker queue has a usable transfer denominator.

    Bisync opens another queue for the opposite direction. Its initial total is
    unknown, so a per-queue percentage must never be labelled overall completion.
    The process exit status, not its byte counter, determines run completion.
    """
    def __init__(self, command):
        self.command = command
        self.phase = "scanning"
        self.direction = None
        self.ready = False
        self.totals_known = False
        self.updated = None
        self.problem = None
        self.problems = []
        self.recovery_required = False
        self.iwork_pending = set()
        self.unknown_bytes = False
        self.stats = {k: 0 for k in COUNTERS}
        self.stats.update(eta=None, transferring=[], checking=[])

    def record_problem(self, problem):
        if not problem:
            return
        if self.problem is None:
            self.problem = problem
        # Bisync repeats the underlying error without its file name at exit.
        if not problem["path"] and any(p["code"] == problem["code"] for p in self.problems):
            return
        if problem not in self.problems and len(self.problems) < 32:
            self.problems.append(problem)

    def update_stats(self, data, sanitize=str):
        if not isinstance(data, dict) or not any(number(data.get(k)) for k in COUNTERS):
            return
        for key in COUNTERS:
            if number(data.get(key)):
                self.stats[key] = max(0, data[key])
        for key in ("fatalError", "retryError"):
            if isinstance(data.get(key), bool):
                self.stats[key] = data[key]
        if "eta" in data:
            self.stats["eta"] = max(0, data["eta"]) if number(data["eta"]) else None
        if "transferring" in data:
            files = []
            for file in (data["transferring"] if isinstance(data["transferring"], list) else [])[:16]:
                if not isinstance(file, dict):
                    continue
                item = {k: file[k] for k in ("bytes", "size", "speed", "speedAvg", "eta", "percentage") if number(file.get(k))}
                item["name"] = sanitize(str(file.get("name", "")))[:2000]
                files.append(item)
            self.stats["transferring"] = files
        if "checking" in data:
            self.stats["checking"] = [sanitize(str(name))[:2000] for name in (data["checking"] if isinstance(data["checking"], list) else [])[:16]]
        if data.get("lastError"):
            self.stats["lastError"] = sanitize(str(data["lastError"]))[:2000]
        self.updated = datetime.now(timezone.utc).isoformat()
        if self.ready and any(number(data.get(k)) for k in ("totalBytes", "totalTransfers")):
            self.totals_known = True

    def observe(self, record, sanitize=str):
        self.update_stats(record.get("stats"), sanitize)
        source = record.get("source", "")
        message = re.sub(r"\x1b\[[0-9;]*m", "", record.get("msg", ""))
        if record.get("skipped") == "copy" and number(record.get("size")) and record["size"] < 0:
            self.unknown_bytes = True
        if "bridge_iwork.go" in source:
            name = sanitize(str(record.get("object", "")))[:2000]
            if message.startswith("Preparing complete iWork download"):
                self.iwork_pending.add(name)
            elif message.startswith("Complete iWork download ready"):
                self.iwork_pending.discard(name)
        if record.get("level") in ("error", "fatal"):
            self.stats["lastError"] = sanitize(message)[:2000]
            self.record_problem(classify_failure(message, record.get("object", ""), record.get("objectType", ""), sanitize))
            if "must run --resync" in message.lower():
                self.recovery_required = True
        if source.startswith("bisync/"):
            if any(text in message for text in ("Resync is copying files to", "Do queued copies to", "Copying Path2 files to Path1")):
                paths = re.findall(r"\bPath([12])\b", message)
                if len(paths) >= 2:
                    self.direction = "nextcloud_to_icloud" if paths[0] == "2" else "icloud_to_nextcloud"
                self.phase, self.ready, self.totals_known = "scanning", False, False
            elif message.startswith("Building Path1 and Path2 listings"):
                self.phase, self.ready, self.totals_known = "scanning", False, False
            elif "checking for diffs" in message or message.startswith(("Checking access health", "Applying changes")):
                self.phase, self.ready, self.totals_known = "comparing", False, False
            elif message.startswith(("Updating listings", "Resync updating listings", "Removing empty directories", "Bisync successful", "No changes found")):
                self.phase, self.ready, self.totals_known = "finalizing", False, False
        elif source.startswith("sync/"):
            if message == "Running all checks before starting transfers":
                self.phase, self.ready, self.totals_known = "scanning", False, False
            elif message == "Checks finished, now starting transfers":
                self.phase, self.ready, self.totals_known = "transferring", True, False
            elif message == "There was nothing to transfer":
                self.phase, self.ready, self.totals_known = "finalizing", False, False
        elif source.startswith("cmd/") and re.match(r"Attempt \d+/\d+ failed", message):
            self.phase, self.ready, self.totals_known = "retrying", False, False

    def snapshot(self):
        percent = None
        phase = self.phase
        unknown = self.unknown_bytes or any(file.get("size", 0) < 0 for file in self.stats["transferring"])
        if self.phase == "transferring" and self.totals_known:
            done, total = self.stats["bytes"], self.stats["totalBytes"]
            if not total or unknown:
                done, total = self.stats["transfers"], self.stats["totalTransfers"]
            if total and done < total:
                percent = math.floor(1000 * done / total) / 10
            elif total and done >= total:
                # Bytes can finish before metadata, the opposite direction or validation.
                phase = "finishing"
        if self.iwork_pending:
            phase, percent = "iwork_download", None
        return {"phase": phase, "direction": self.direction, "percent": percent, "problem": self.problem,
                "problems": list(self.problems), "recovery_required": self.recovery_required,
                "scope": "known_transfers", "totals_known": self.totals_known, "updated": self.updated,
                "bytes_known": not unknown, "iwork_downloads": sorted(self.iwork_pending)[:16]}


def finish_progress(run):
    progress = run.setdefault("progress", {})
    progress.update(phase=run["status"], percent=100 if run["status"] == "success" else None,
                    scope="run", updated=datetime.now(timezone.utc).isoformat())
    if run["status"] == "success":
        progress["totals_known"] = True
    run.get("stats", {}).update(transferring=[], checking=[], speed=0, eta=None)
