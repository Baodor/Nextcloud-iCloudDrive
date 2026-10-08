import json
import base64
import hashlib
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from cryptography.fernet import Fernet
from bridge.models import BridgeError, validate_job, path, tenant, next_due, make_command
from bridge.store import Store
from bridge.engine import Engine
from bridge.server import handler_class
from bridge.rclone import redact
from bridge.dav import rewrite_multistatus
from xml.etree import ElementTree


class FakeRC:
    version = "v1.75.1-icloud-bridge.1"
    def __init__(self):
        self.calls = []
        self.process = type("Process", (), {"poll": lambda self: None})()
    def call(self, endpoint, payload=None, **kw):
        self.calls.append((endpoint, payload or {}))
        if endpoint == "core/version": return {"version": self.version}
        if endpoint == "operations/list": return {"list": []}
        if endpoint == "job/status": return {"finished": True, "output": {"result": "dry run log", "error": False}}
        return {}
    def command(self, command, args, options=None, **kw):
        self.calls.append((command, {"args": args, "options": options}))
        return {"jobid": 1}
    def start_transfer(self, command, args, options):
        self.command(command, args, options)
        return type("Transfer", (), {"poll": lambda self: 0, "log": lambda self: "dry run log", "stats": lambda self: {}, "progress": lambda self: {"phase": "transferring", "percent": None}, "cancel": lambda self: None})()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Fernet.generate_key().decode())
        self.rc = FakeRC()
        self.engine = Engine(self.store, self.rc, self.root, "https://cloud.example.com")
        self.store.save_user("alice", {"icloud_connected": True, "nextcloud_connected": True}, {"dav_password": "sensitive"})
        self.data = {"name": "Documents", "icloud_path": "Documents", "nextcloud_path": "iCloud/Documents"}
    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()
    def job(self, **kw):
        return self.engine.save_job("alice", {**self.data, **kw})
    def test_rejects_traversal_remote_and_shell_settings(self):
        for value in ["../etc", "a/../b", "a//b", "x:y", "a\\b", "a\n--force", "."]:
            with self.subTest(value=value), self.assertRaises(BridgeError): path(value)
        for data in [{"transfers": True}, {"bandwidth": "10M --force"}, {"excludes": ["foo\n+ **"]}, {"max_delete_percent": 99}]:
            with self.assertRaises(BridgeError): self.job(**data)
    def test_disallows_entire_roots_and_overlapping_jobs(self):
        with self.assertRaises(BridgeError): self.job(icloud_path="")
        self.job()
        with self.assertRaises(BridgeError): self.job(icloud_path="Documents/Sub", nextcloud_path="Elsewhere")
        with self.assertRaises(BridgeError): self.job(icloud_path="Elsewhere", nextcloud_path="iCloud/documents")
    def test_tenant_isolation(self):
        job = self.job()
        with self.assertRaises(BridgeError): self.engine.queue_run("bob", job["id"], "preview")
        with self.assertRaises(BridgeError): self.engine.delete_job("bob", job["id"])
        self.assertNotEqual(tenant("alice"), tenant("bob"))
    def test_secrets_are_encrypted_at_rest(self):
        row = self.store.db.execute("SELECT secret FROM users WHERE uid='alice'").fetchone()[0]
        self.assertNotIn("sensitive", row)
        self.assertEqual(self.store.user("alice")[1]["dav_password"], "sensitive")
    def test_download_cache_cleanup_only_removes_the_selected_account(self):
        entries = {}
        for uid in ("alice", "bob"):
            namespace = hashlib.sha256(self.engine.remote(uid, "icloud")[:-1].encode()).hexdigest()
            directory = self.root / "cache" / "iwork-downloads" / namespace
            directory.mkdir(parents=True)
            entries[uid] = directory / "version.zip"
            entries[uid].write_bytes(b"private document")
        self.engine.disconnect("alice", "icloud")
        self.assertFalse(entries["alice"].exists())
        self.assertEqual(entries["bob"].read_bytes(), b"private document")
    def test_download_size_capability_requires_the_corrected_rclone_build(self):
        self.assertEqual(self.engine.state("alice")["capabilities"]["iwork_download_size"], 1)
        self.rc.version = "v1.75.1"
        older = Engine(self.store, self.rc, self.root, "https://cloud.example.com")
        self.assertEqual(older.state("alice")["capabilities"]["iwork_download_size"], 0)
    def test_initialization_requires_successful_preview(self):
        job = self.job()
        with self.assertRaises(BridgeError): self.engine.queue_run("alice", job["id"], "run")
        with self.assertRaises(BridgeError): self.engine.queue_run("alice", job["id"], "initialize")
    def test_preview_does_not_modify_real_checkpoints_or_create_markers(self):
        job = self.job(enabled=True)
        base = self.root / "state" / job["id"]
        base.mkdir(parents=True)
        (base / "checkpoint.lst").write_text("original")
        run = self.engine.queue_run("alice", job["id"], "preview")
        saved = self.store.get("runs", "alice", run["id"])
        self.engine.execute("alice", saved)
        self.assertEqual((base / "checkpoint.lst").read_text(), "original")
        self.assertFalse(self.store.get("jobs", "alice", job["id"])["initialized"])
        self.assertFalse(any(e == "operations/copyfile" for e, _ in self.rc.calls))
        init = self.engine.queue_run("alice", job["id"], "initialize")
        self.engine.execute("alice", self.store.get("runs", "alice", init["id"]))
        self.assertTrue(self.store.get("jobs", "alice", job["id"])["initialized"])
        self.assertEqual(sum(e == "operations/copyfile" for e, _ in self.rc.calls), 2)
    def test_regular_command_never_resyncs_and_retains_conflicts(self):
        job = self.job()
        command, args, opts = make_command(job, self.engine.refs("alice", job), self.root, "run", "run")
        self.assertEqual(command, "bisync")
        self.assertNotIn("resync", opts)
        self.assertNotIn("resync-mode", opts)
        self.assertEqual(opts["conflict-loser"], "num")
        self.assertEqual(opts["conflict-resolve"], "none")
        self.assertIn("backup-dir1", opts)
    def test_copy_mode_never_deletes(self):
        job = self.job(mode="download")
        command, _, opts = make_command(job, self.engine.refs("alice", job), self.root, "run", "run")
        self.assertEqual(command, "copy")
        self.assertEqual(opts["check-first"], "true")
        self.assertNotIn("max-delete", opts)
        self.assertIn("backup-dir", opts)
    def test_filter_change_invalidates_initialized_job(self):
        job = self.job()
        job["initialized"] = True
        self.store.put("jobs", "alice", job)
        changed = self.engine.save_job("alice", {**job, "excludes": ["*.tmp"]}, job["id"])
        self.assertFalse(changed["initialized"])
        self.assertIsNone(changed["next_run"])
    def test_schedules_respect_weekdays_timezone_and_initialization(self):
        job = self.job(enabled=True, days=[0], time="03:00")
        self.assertIsNone(next_due(job))
        job["initialized"] = True
        self.assertEqual(next_due(job, datetime(2026, 10, 4, 12, tzinfo=timezone.utc)), "2026-10-05T01:00:00+00:00")
    def test_spring_dst_time_is_normalized(self):
        job = self.job(mode="download", enabled=True, days=[6], time="02:30")
        self.assertEqual(next_due(job, datetime(2026, 3, 28, 20, tzinfo=timezone.utc)), "2026-03-29T01:30:00+00:00")
    def test_cancel_request_survives_progress_update(self):
        job = self.job()
        run = self.engine.queue_run("alice", job["id"], "preview")
        run = self.store.get("runs", "alice", run["id"])
        run["status"] = "running"
        self.store.put("runs", "alice", run)
        self.engine.cancel("alice", run["id"])
        self.store.put("runs", "alice", run)
        self.assertTrue(self.store.get("runs", "alice", run["id"])["cancel_requested"])
    def test_redacts_auth_values(self):
        text = redact('Bearer abc password: secret token=private', ["private", "secret"])
        self.assertNotIn("abc", text)
        self.assertNotIn("private", text)
        self.assertNotIn("secret", text)
    def test_webdav_resource_urls_stay_in_tenant_gateway(self):
        body = b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/</D:href></D:response><D:response><D:href>/My%20Files/report.txt</D:href></D:response><D:response><D:href>http://127.0.0.1:1234/Other/</D:href></D:response></D:multistatus>'
        xml = ElementTree.fromstring(rewrite_multistatus(body, "/dav/alice/"))
        self.assertEqual([node.text for node in xml.iter("{DAV:}href")],
                         ["/dav/alice/", "/dav/alice/My%20Files/report.txt", "/dav/alice/Other/"])
        with self.assertRaises(BridgeError): rewrite_multistatus(b"invalid", "/dav/alice/")
    def test_http_authentication_and_user_boundary(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.engine, "t" * 48))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/state"
        try:
            with self.assertRaises(HTTPError) as caught: urlopen(url)
            self.assertEqual(caught.exception.code, 401)
            req = Request(url, headers={"Authorization": "Bearer " + "t" * 48, "X-Bridge-User": base64.b64encode("bob".encode()).decode()})
            with urlopen(req) as response: data = json.load(response)
            self.assertFalse(data["connection"]["icloud_connected"])
            self.assertEqual(data["jobs"], [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__": unittest.main()
