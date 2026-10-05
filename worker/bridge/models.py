"""Validate every setting before it can become an rclone argument."""
from datetime import datetime, timedelta, timezone
import hashlib
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MARKER = ".icloud-bridge-check"
BACKUPS = "iCloud Bridge Backups"
MODES = {"bisync", "download", "upload"}


class BridgeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def tenant(uid):
    if not isinstance(uid, str) or not uid or len(uid) > 255:
        raise BridgeError("Missing or invalid Nextcloud user identity.", 401)
    return hashlib.sha256(uid.encode()).hexdigest()[:32]


def path(value, allow_root=True):
    if not isinstance(value, str) or len(value) > 1024:
        raise BridgeError("Invalid folder path.")
    value = unicodedata.normalize("NFC", value).strip("/")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or ":" in value or "\\" in value:
        raise BridgeError("Folder paths must not contain control characters, colons or backslashes.")
    if value and any(p in {"", ".", ".."} for p in value.split("/")):
        raise BridgeError("Folder paths must not contain empty, dot or parent components.")
    if not allow_root and not value:
        raise BridgeError("Select a folder, not the entire drive root.")
    return value


def overlap(a, b):
    a, b = a.casefold(), b.casefold()
    return not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/")


def integer(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise BridgeError(f"{key} must be an integer from {low} to {high}.")
    return value


def boolean(data, key, default):
    value = data.get(key, default)
    if type(value) is not bool:
        raise BridgeError(f"{key} must be true or false.")
    return value


def validate_job(data):
    if not isinstance(data, dict):
        raise BridgeError("Expected a job object.")
    name = data.get("name", "Folder sync")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise BridgeError("Choose a job name with 1–100 characters.")
    cloud, nc = path(data.get("icloud_path", ""), False), path(data.get("nextcloud_path", ""), False)
    if overlap(cloud, BACKUPS) or overlap(nc, BACKUPS):
        raise BridgeError("The backup folder cannot be synchronized.")
    mode = data.get("mode", "bisync")
    conflict = data.get("conflict", "keep_both")
    initial = data.get("initial", "icloud")
    schedule = data.get("schedule", "daily")
    if mode not in MODES or conflict not in {"keep_both", "newer", "icloud", "nextcloud"}:
        raise BridgeError("Invalid sync mode or conflict policy.")
    if initial not in {"icloud", "nextcloud", "newer"} or schedule not in {"manual", "daily", "interval"}:
        raise BridgeError("Invalid initialization or schedule setting.")
    zone = data.get("timezone", "Europe/Berlin")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise BridgeError("Use a valid IANA time zone, such as Europe/Berlin.")
    clock = data.get("time", "03:00")
    if not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise BridgeError("Use a time in HH:MM format.")
    days = data.get("days", list(range(7)))
    if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
        raise BridgeError("Select at least one weekday (Monday=0, Sunday=6).")
    limit = data.get("bandwidth", "")
    if not isinstance(limit, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?[kKmMgG]?)?", limit):
        raise BridgeError("Bandwidth must be empty or a value such as 10M.")
    excludes = data.get("excludes", [".DS_Store", "._*", "~$*"])
    if not isinstance(excludes, list) or len(excludes) > 100:
        raise BridgeError("Use at most 100 exclude patterns.")
    for pattern in excludes:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 500 or any(ord(c) < 32 for c in pattern):
            raise BridgeError("Each exclude pattern must be one non-empty line.")
    return {
        "name": name.strip(), "icloud_path": cloud, "nextcloud_path": nc, "mode": mode,
        "conflict": conflict, "initial": initial, "schedule": schedule, "timezone": zone,
        "time": clock, "days": sorted(set(days)), "bandwidth": limit, "excludes": excludes,
        "enabled": boolean(data, "enabled", False), "backup": boolean(data, "backup", True),
        "iwork_packages": boolean(data, "iwork_packages", True),
        "empty_dirs": boolean(data, "empty_dirs", True),
        "interval_minutes": integer(data, "interval_minutes", 1440, 60, 10080),
        "max_delete_percent": integer(data, "max_delete_percent", 10, 0, 50),
        "transfers": integer(data, "transfers", 2, 1, 8),
        "checkers": integer(data, "checkers", 4, 1, 16),
        "retries": integer(data, "retries", 3, 1, 10),
        "timeout_minutes": integer(data, "timeout_minutes", 60, 5, 1440),
    }


def next_due(job, now=None):
    now = now or datetime.now(timezone.utc)
    if not job["enabled"] or job["schedule"] == "manual":
        return None
    if job["mode"] == "bisync" and not job.get("initialized"):
        return None
    if job["schedule"] == "interval":
        return (now + timedelta(minutes=job["interval_minutes"])).isoformat()
    zone = ZoneInfo(job["timezone"])
    local = now.astimezone(zone)
    hour, minute = map(int, job["time"].split(":"))
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        # Normalize nonexistent spring-forward wall times to the next valid instant.
        candidate = candidate.astimezone(timezone.utc).astimezone(zone)
        if candidate.weekday() in job["days"] and candidate > local:
            return candidate.astimezone(timezone.utc).isoformat()
    raise BridgeError("Cannot compute next scheduled run.")


def make_command(job, refs, workdir, run_id, action):
    preview = action == "preview"
    initialize = action == "initialize" or (preview and not job.get("initialized"))
    opts = {"log-level": "INFO", "use-json-log": "true", "transfers": str(job["transfers"]),
            "checkers": str(job["checkers"]), "retries": str(job["retries"]), "timeout": "2m",
            "check-first": "true"}
    if preview:
        opts["dry-run"] = "true"
    if job["bandwidth"]:
        opts["bwlimit"] = job["bandwidth"]
    if job["empty_dirs"]:
        opts["create-empty-src-dirs"] = "true"
    backup = f"{BACKUPS}/{job['id']}/{run_id}"
    if job["mode"] == "bisync":
        opts.update({"workdir": str(workdir), "compare": "size,modtime", "check-access": "true",
                     "check-filename": MARKER, "max-delete": str(job["max_delete_percent"]),
                     "conflict-resolve": {"keep_both": "none", "newer": "newer", "icloud": "path1", "nextcloud": "path2"}[job["conflict"]],
                     "conflict-loser": "num", "resilient": "true", "recover": "true",
                     "filters-file": str(workdir / "filters.txt")})
        if initialize:
            opts["resync-mode"] = {"icloud": "path1", "nextcloud": "path2", "newer": "newer"}[job["initial"]]
        if job["backup"] and not preview:
            opts["backup-dir1"] = refs["icloud_root"] + backup
            opts["backup-dir2"] = refs["nextcloud_root"] + backup
        return "bisync", [refs["icloud"], refs["nextcloud"]], opts
    source, destination = ("icloud", "nextcloud") if job["mode"] == "download" else ("nextcloud", "icloud")
    opts["filter-from"] = str(workdir / "filters.txt")
    if job["backup"] and not preview:
        opts["backup-dir"] = refs[destination + "_root"] + backup
    return "copy", [refs[source], refs[destination]], opts
