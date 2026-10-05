"""Durable scheduling and serialized transfers with explicit initialization."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
import json
import queue
import secrets
import shutil
import threading
import time
import uuid
from .models import BridgeError, MARKER, tenant, path, overlap, validate_job, next_due, make_command
from .rclone import redact
from .progress import finish_progress
from .iwork import IWork, recover as recover_iwork


def now():
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, store, rc, root, nextcloud_url):
        parsed = urlsplit(nextcloud_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise RuntimeError("NEXTCLOUD_URL must be a server base URL without credentials, query or fragment.")
        self.store, self.rc, self.root, self.nc_url = store, rc, root, nextcloud_url.rstrip("/")
        self.lock = threading.RLock()
        self.transfers = queue.Queue()
        self.stop_event = threading.Event()
        self.active = None
        self.transfer = None
        self.dav = None
        self.version = rc.call("core/version").get("version", "unknown")
        # An interrupted process cannot prove its last changes: pause and retain checkpoints.
        for run in store.records("runs"):
            if run.get("iwork", {}).get("pending"):
                try:
                    recover_iwork(self, run["uid"], run)
                except Exception as error:
                    run.update(status="interrupted", finished=now(), error=redact(str(error)))
                    finish_progress(run)
                    store.put("runs", run["uid"], run)
                    job = store.get("jobs", run["uid"], run["job_id"])
                    if job:
                        job.update(enabled=False, initialized=False, next_run=None)
                        store.put("jobs", run["uid"], job)
                    continue
            if run["status"] in {"running", "queued"}:
                run.update(status="interrupted", finished=now(), error="Worker restarted during this run. Review and initialize again if required.")
                finish_progress(run)
                store.put("runs", run["uid"], run)
                job = store.get("jobs", run["uid"], run["job_id"])
                if job:
                    job.update(enabled=False, next_run=None)
                    store.put("jobs", run["uid"], job)

    def start(self):
        self.runner_thread = threading.Thread(target=self.runner, daemon=True)
        self.runner_thread.start()
        threading.Thread(target=self.scheduler, daemon=True).start()

    def invalidate_account_jobs(self, uid):
        for job in self.store.records("jobs", uid):
            job.update(initialized=False, enabled=False, next_run=None, updated=now())
            old = self.root / "state" / job["id"]
            if old.exists():
                old.rename(old.with_name(job["id"] + "-retired-" + uuid.uuid4().hex[:8]))
            self.store.put("jobs", uid, job)

    def remote(self, uid, side):
        return f"{side}_{tenant(uid)}:"

    def busy(self, uid):
        return any(r["status"] in {"running", "queued"} for r in self.store.records("runs", uid))

    def require_idle(self, uid):
        if self.busy(uid):
            raise BridgeError("Wait for the active run or stop it before changing connections or folder mappings.", 409)

    def refs(self, uid, job):
        return {"icloud": self.remote(uid, "icloud") + job["icloud_path"],
                "nextcloud": self.remote(uid, "nextcloud") + job["nextcloud_path"],
                "icloud_root": self.remote(uid, "icloud"), "nextcloud_root": self.remote(uid, "nextcloud")}

    def state(self, uid):
        public, _ = self.store.user(uid)
        runs = sorted(self.store.records("runs", uid), key=lambda r: r["started"], reverse=True)
        return {"connection": public, "jobs": self.store.records("jobs", uid),
                "runs": [self.public_run(r) for r in runs[:30]], "rclone_version": self.version,
                "nextcloud_user": uid, "version": "0.1.0"}

    @staticmethod
    def public_run(run):
        return {k: v for k, v in run.items() if k not in {"uid", "rc_job", "group"}}

    def auth_result(self, uid, result, public, secret):
        result = result.get("result", result)
        if not isinstance(result, dict):
            raise BridgeError("Unexpected rclone authentication response.", 502)
        state = result.get("State", result.get("state", ""))
        option = result.get("Option", result.get("option")) or {}
        error = result.get("Error", result.get("error", ""))
        if state:
            secret["auth_state"] = state
            secret["auth_expires"] = time.time() + 900
            self.store.save_user(uid, public, secret)
            return {"complete": False, "question": {
                "name": option.get("Name", "answer"), "help": redact(option.get("Help", "Enter the verification code.")),
                "password": bool(option.get("IsPassword")), "default": option.get("Default", ""),
                "examples": option.get("Examples", []), "error": redact(str(error))}}
        # Confirm that authentication also permits Drive access, including PCS approval.
        self.rc.call("fscache/clear")
        self.rc.call("operations/list", {"fs": self.remote(uid, "icloud"), "remote": "", "opt": {"dirsOnly": True}})
        public.update(icloud_connected=True, authenticated_at=now(),
                      reauthenticate_after=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
        secret.pop("auth_state", None)
        secret.pop("auth_parameters", None)
        secret.pop("auth_expires", None)
        secret.setdefault("dav_password", secrets.token_urlsafe(32))
        self.store.save_user(uid, public, secret)
        return {"complete": True}

    def connect_icloud(self, uid, data):
        with self.lock:
            self.require_idle(uid)
            email, password = data.get("apple_id", ""), data.get("password", "")
            if not isinstance(email, str) or "@" not in email or len(email) > 254 or not isinstance(password, str) or not 1 <= len(password) <= 1024:
                raise BridgeError("Enter your Apple account and regular Apple password.")
            public, secret = self.store.user(uid)
            if public.get("apple_id") and public["apple_id"].casefold() != email.casefold():
                self.invalidate_account_jobs(uid)
            public.update(icloud_connected=False, apple_id=email)
            parameters = {"apple_id": email, "password": password, "service": "drive"}
            secret["auth_parameters"] = parameters
            self.store.save_user(uid, public, secret)
            if self.dav:
                self.dav.close_user(uid)
            name = self.remote(uid, "icloud")[:-1]
            self.rc.call("config/delete", {"name": name})
            try:
                result = self.rc.call("config/create", {"name": name, "type": "iclouddrive", "parameters": parameters,
                                                        "opt": {"nonInteractive": True, "obscure": True}})
                return self.auth_result(uid, result, public, secret)
            except BridgeError as e:
                raise BridgeError(redact(str(e), [password]), e.status)

    def continue_auth(self, uid, data):
        with self.lock:
            self.require_idle(uid)
            public, secret = self.store.user(uid)
            if not secret.get("auth_state") or secret.get("auth_expires", 0) < time.time():
                raise BridgeError("Authentication expired. Start the Apple login again.", 409)
            answer = data.get("answer", "")
            if not isinstance(answer, str) or len(answer) > 1024:
                raise BridgeError("Invalid authentication answer.")
            try:
                result = self.rc.call("config/update", {"name": self.remote(uid, "icloud")[:-1],
                    "parameters": secret["auth_parameters"],
                    "opt": {"nonInteractive": True, "obscure": True, "continue": True, "state": secret["auth_state"], "result": answer}})
                return self.auth_result(uid, result, public, secret)
            except BridgeError as error:
                raise BridgeError(redact(str(error), [answer, secret["auth_parameters"].get("password", "")]), error.status)

    def connect_nextcloud(self, uid, data):
        with self.lock:
            self.require_idle(uid)
            username, password = data.get("username", uid), data.get("password", "")
            if not isinstance(username, str) or not username or len(username) > 255 or not isinstance(password, str) or not 1 <= len(password) <= 1024:
                raise BridgeError("Enter the Nextcloud login ID and an app password.")
            public, secret = self.store.user(uid)
            if public.get("nextcloud_username") and public["nextcloud_username"] != username:
                self.invalidate_account_jobs(uid)
            public.update(nextcloud_connected=False)
            self.store.save_user(uid, public, secret)
            parameters = {"url": self.nc_url + "/remote.php/dav/files/" + quote(username, safe="") + "/",
                          "vendor": "nextcloud", "user": username, "pass": password}
            try:
                self.rc.call("config/create", {"name": self.remote(uid, "nextcloud")[:-1], "type": "webdav",
                                                "parameters": parameters, "opt": {"nonInteractive": True, "obscure": True}})
                self.rc.call("fscache/clear")
                self.rc.call("operations/list", {"fs": self.remote(uid, "nextcloud"), "remote": "", "opt": {"dirsOnly": True}})
            except BridgeError as e:
                raise BridgeError(redact(str(e), [password]), e.status)
            public.update(nextcloud_connected=True, nextcloud_username=username)
            self.store.save_user(uid, public, secret)
            return {"connected": True}

    def folders(self, uid, side, folder):
        if side not in {"icloud", "nextcloud"}:
            raise BridgeError("Invalid storage side.")
        folder = path(folder)
        public, _ = self.store.user(uid)
        if not public.get(side + "_connected"):
            raise BridgeError("Connect this account first.", 409)
        result = self.rc.call("operations/list", {"fs": self.remote(uid, side), "remote": folder, "opt": {"dirsOnly": True}})
        entries = [{"name": i["Name"], "path": path(i["Path"])} for i in result.get("list", []) if i.get("IsDir")]
        return {"path": folder, "folders": sorted(entries, key=lambda f: f["name"].casefold())}

    def mkdir(self, uid, folder):
        public, _ = self.store.user(uid)
        if not public.get("nextcloud_connected"):
            raise BridgeError("Connect Nextcloud first.", 409)
        folder = path(folder, False)
        self.rc.call("operations/mkdir", {"fs": self.remote(uid, "nextcloud"), "remote": folder})
        return {"created": folder}

    def save_job(self, uid, data, identifier=None):
        with self.lock:
            self.require_idle(uid)
            validated = validate_job(data)
            old = self.store.get("jobs", uid, identifier) if identifier else None
            if identifier and not old:
                raise BridgeError("Job not found.", 404)
            for other in self.store.records("jobs", uid):
                if other["id"] != identifier and (overlap(validated["icloud_path"], other["icloud_path"]) or overlap(validated["nextcloud_path"], other["nextcloud_path"])):
                    raise BridgeError("This folder overlaps another job. Choose separate folders.", 409)
            identity_fields = ("icloud_path", "nextcloud_path", "mode", "excludes", "empty_dirs", "iwork_packages")
            unchanged = bool(old) and all(old.get(k, True if k == "iwork_packages" else None) == validated[k] for k in identity_fields)
            job = {**validated, "id": identifier or uuid.uuid4().hex, "initialized": bool(unchanged and old.get("initialized")),
                   "created": old["created"] if old else now(), "updated": now(), "last_success": old.get("last_success") if old else None}
            if old and not unchanged:
                original = self.root / "state" / job["id"]
                if original.exists():
                    original.rename(original.with_name(job["id"] + "-retired-" + uuid.uuid4().hex[:8]))
            job["next_run"] = next_due(job)
            self.store.put("jobs", uid, job)
            return job

    def queue_run(self, uid, identifier, action="run"):
        with self.lock:
            job = self.store.get("jobs", uid, identifier)
            if not job:
                raise BridgeError("Job not found.", 404)
            if action not in {"run", "preview", "initialize"}:
                raise BridgeError("Invalid run action.")
            if any(r["job_id"] == identifier and r["status"] in {"queued", "running"} for r in self.store.records("runs", uid)):
                raise BridgeError("This job is already queued or running.", 409)
            if job["mode"] == "bisync" and action == "run" and not job["initialized"]:
                raise BridgeError("Preview and initialize this folder pair before running it.", 409)
            if action == "initialize" and job["mode"] != "bisync":
                raise BridgeError("One-way copy jobs do not need initialization.")
            if action == "initialize" and not any(r["job_id"] == identifier and r["action"] == "preview" and r["status"] == "success" and r.get("finished", "") >= job["updated"] for r in self.store.records("runs", uid)):
                raise BridgeError("Run a successful preview after saving these settings before initializing.", 409)
            public, _ = self.store.user(uid)
            if not public.get("icloud_connected") or not public.get("nextcloud_connected"):
                raise BridgeError("Connect both accounts before synchronizing.", 409)
            run = {"id": uuid.uuid4().hex, "uid": uid, "job_id": identifier, "job_name": job["name"],
                   "action": action, "status": "queued", "started": now(), "finished": None, "log": "", "stats": {},
                   "progress": {"phase": "queued", "percent": None}}
            self.store.put("runs", uid, run)
            self.transfers.put((uid, run["id"]))
            return self.public_run(run)

    def prepare(self, uid, job, run, deadline=None):
        # A failed MOVE can leave a durable recovery journal. Restore any missing
        # package before another run reads these folders or considers deletions.
        with self.lock:
            for previous in self.store.records("runs", uid):
                if previous.get("iwork", {}).get("pending"):
                    recover_iwork(self, uid, previous)
        refs = self.refs(uid, job)
        # Always list real, unfiltered roots before writing or interpreting deletions.
        for side in ("icloud", "nextcloud"):
            run["progress"].update(phase="preparing", side=side)
            self.store.put("runs", uid, run)
            self.rc.call("operations/list", {"fs": refs[side], "remote": ""})
        iwork = IWork(self, uid, job, run, refs, deadline)
        iwork.plan()
        iwork.normalize()
        base = self.root / "state" / job["id"]
        if run["action"] == "preview":
            work = self.root / "previews" / run["id"]
            if base.exists():
                shutil.copytree(base, work)
            else:
                work.mkdir(parents=True)
        else:
            work = base
            work.mkdir(parents=True, exist_ok=True)
        # Initialization preview cannot create markers. Check-access applies to regular runs.
        initializing_preview = run["action"] == "preview" and not job["initialized"]
        if job["mode"] == "bisync" and run["action"] == "initialize":
            for side in ("icloud", "nextcloud"):
                self.rc.call("operations/copyfile", {"srcFs": str(Path(__file__).resolve().parents[1]), "srcRemote": "access-check.txt",
                                                     "dstFs": refs[side], "dstRemote": MARKER})
        excludes = job["excludes"] + (iwork.preview_filters() if run["action"] == "preview" else [])
        work.joinpath("filters.txt").write_text("+ " + MARKER + "\n" + "".join("- " + e + "\n" for e in excludes), encoding="utf-8")
        command, args, opts = make_command(job, refs, work, run["id"], run["action"])
        if initializing_preview:
            opts.pop("check-access", None)
        return command, args, opts, work

    def execute(self, uid, run):
        job = self.store.get("jobs", uid, run["job_id"])
        if not job:
            return
        run.update(status="running", actual_started=now(), progress={"phase": "preparing", "percent": None})
        self.active = run["id"]
        self.store.put("runs", uid, run)
        work = None
        try:
            public, _ = self.store.user(uid)
            if not public.get("icloud_connected") or not public.get("nextcloud_connected"):
                raise BridgeError("A connection was removed before this job started.", 409)
            deadline = time.monotonic() + job["timeout_minutes"] * 60
            command, args, opts, work = self.prepare(uid, job, run, deadline)
            with self.lock:
                if not self.store.get("runs", uid, run["id"]):
                    raise BridgeError("This bridge account was removed.", 409)
                transfer = self.rc.start_transfer(command, args, opts)
                self.transfer = transfer
            while True:
                current = self.store.get("runs", uid, run["id"])
                if not current or current.get("cancel_requested") or self.stop_event.is_set() or time.monotonic() > deadline:
                    transfer.cancel()
                    run["cancel_requested"] = True
                code = transfer.poll()
                run["log"] = transfer.log()
                stats = transfer.stats()
                if stats:
                    run["stats"] = stats
                run["progress"] = transfer.progress()
                if not run["progress"].get("direction") and job["mode"] != "bisync":
                    run["progress"]["direction"] = "icloud_to_nextcloud" if job["mode"] == "download" else "nextcloud_to_icloud"
                if code is not None:
                    if run.get("cancel_requested"):
                        raise BridgeError("Run stopped. Review the folders before restarting.", 409)
                    if code:
                        raise BridgeError(f"rclone exited with code {code}. Inspect the run log for details.", 502)
                    break
                with self.lock:
                    if self.store.get("runs", uid, run["id"]):
                        self.store.put("runs", uid, run)
                self.stop_event.wait(1)
            run["status"] = "success"
            if run["action"] != "preview":
                if run["action"] == "initialize":
                    job["initialized"] = True
                job["last_success"] = now()
        except Exception as error:
            run.update(status="stopped" if run.get("cancel_requested") else "failed", error=redact(str(error)))
            if run["action"] != "preview":
                job.update(enabled=False)
                # Recovery must be reviewed, never silently replace checkpoints with --resync.
                if job["mode"] == "bisync":
                    job["initialized"] = False
        finally:
            run["finished"] = now()
            finish_progress(run)
            job["next_run"] = next_due(job)
            with self.lock:
                if self.store.get("jobs", uid, job["id"]):
                    self.store.put("jobs", uid, job)
                if self.store.get("runs", uid, run["id"]):
                    self.store.put("runs", uid, run)
                self.active = None
                self.transfer = None
            if run["action"] == "preview" and work:
                shutil.rmtree(work, ignore_errors=True)
            records = sorted(self.store.records("runs", uid), key=lambda r: r["started"], reverse=True)
            for old in records[100:]:
                if old["status"] not in {"running", "queued"} and not old.get("iwork", {}).get("pending"):
                    self.store.delete("runs", uid, old["id"])

    def runner(self):
        while not self.stop_event.is_set():
            try:
                uid, identifier = self.transfers.get(timeout=1)
            except queue.Empty:
                continue
            run = self.store.get("runs", uid, identifier)
            if run and run["status"] == "queued":
                self.execute(uid, run)
            self.transfers.task_done()

    def scheduler(self):
        while not self.stop_event.wait(15):
            for job in self.store.records("jobs"):
                if not job.get("next_run") or job["next_run"] > now():
                    continue
                # Look up ownership from the database, not untrusted job settings.
                with self.store.lock:
                    row = self.store.db.execute("SELECT uid FROM jobs WHERE id=?", (job["id"],)).fetchone()
                if row:
                    try:
                        self.queue_run(row[0], job["id"])
                    except BridgeError:
                        pass

    def cancel(self, uid, identifier):
        with self.lock:
            run = self.store.get("runs", uid, identifier)
            if not run:
                raise BridgeError("Run not found.", 404)
            if run["status"] == "queued":
                run.update(status="stopped", finished=now())
                finish_progress(run)
            elif run["status"] == "running":
                run["cancel_requested"] = True
            self.store.put("runs", uid, run)
            return self.public_run(run)

    def disconnect(self, uid, side):
        with self.lock:
            self.require_idle(uid)
            if side not in {"icloud", "nextcloud"}:
                raise BridgeError("Invalid account side.")
            public, secret = self.store.user(uid)
            public[side + "_connected"] = False
            if side == "icloud":
                secret = {k: v for k, v in secret.items() if not k.startswith("auth_") and k != "dav_password"}
                if self.dav:
                    self.dav.close_user(uid)
            self.rc.call("config/delete", {"name": self.remote(uid, side)[:-1]})
            self.store.save_user(uid, public, secret)
            for job in self.store.records("jobs", uid):
                job.update(enabled=False, next_run=None)
                self.store.put("jobs", uid, job)
            return {"disconnected": side}

    def purge(self, uid):
        with self.lock:
            for run in self.store.records("runs", uid):
                if run["id"] == self.active and self.transfer:
                    self.transfer.finish_cancel()
            for side in ("icloud", "nextcloud"):
                self.rc.call("config/delete", {"name": self.remote(uid, side)[:-1]})
            self.rc.call("fscache/clear")
            if self.dav:
                self.dav.close_user(uid)
            shutil.rmtree(self.root / "cache" / tenant(uid), ignore_errors=True)
            shutil.rmtree(self.root / "iwork" / tenant(uid), ignore_errors=True)
            for job in self.store.records("jobs", uid):
                for state_dir in (self.root / "state").glob(job["id"] + "*"):
                    shutil.rmtree(state_dir, ignore_errors=True)
            self.store.purge(uid)
            return {"removed": True}

    def delete_job(self, uid, identifier):
        with self.lock:
            self.require_idle(uid)
            if not self.store.get("jobs", uid, identifier):
                raise BridgeError("Job not found.", 404)
            self.store.delete("jobs", uid, identifier)
            shutil.rmtree(self.root / "state" / identifier, ignore_errors=True)
            return {"deleted": True}
