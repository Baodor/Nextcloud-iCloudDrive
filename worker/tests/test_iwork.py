"""Document preservation, guarded commits, previews and recovery without Apple login."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io
import json
import tempfile
import threading
import unittest
import zipfile
from cryptography.fernet import Fernet
from bridge.engine import Engine
from bridge.iwork import IWork, package_paths, recover
from bridge.models import BridgeError, validate_job
from bridge.store import Store
from test_bridge import FakeRC


class MemoryDAV:
    def __init__(self, contents):
        self.files = dict(contents)
        self.directories = set()
        for name in contents:
            parts = name.split("/")
            self.directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
        self.moves, self.writes = [], []
        self.fail_promotion = self.bad_upload = False
        self.revision = '"original"'

    def stat(self, path):
        if path in self.directories:
            return {"directory": True, "etag": self.revision, "mtime": 1700000000, "size": 0}
        if path in self.files:
            return {"directory": False, "etag": '"file"', "mtime": 1700000000, "size": len(self.files[path])}
        return None

    def mkdir_parents(self, path):
        parts = path.split("/")
        self.directories.update("/".join(parts[:i]) for i in range(1, len(parts)))

    def request(self, method, path, data=None, headers=None):
        if method == "GET":
            return io.BytesIO(self.files[path])
        assert method == "PUT"
        self.writes.append(path)
        self.files[path] = data.read()
        if self.bad_upload:
            self.files[path] = b"corrupt upload"
        return io.BytesIO(b"")

    def move(self, source, destination, etag=None, directory=False):
        if not directory and self.fail_promotion:
            raise BridgeError("Promotion failed", 503)
        if self.stat(destination):
            raise BridgeError("Destination already exists", 412)
        if etag and self.stat(source)["etag"] != etag:
            raise BridgeError("Source changed", 412)
        self.moves.append((source, destination))
        if directory:
            for path in list(self.files):
                if path.startswith(source + "/"):
                    self.files[destination + path[len(source):]] = self.files.pop(path)
            for path in list(self.directories):
                if path == source or path.startswith(source + "/"):
                    self.directories.remove(path)
                    self.directories.add(destination + path[len(source):])
        else:
            self.files[destination] = self.files.pop(source)


class PackageRC(FakeRC):
    def __init__(self, contents):
        super().__init__()
        self.contents = contents
        self.cloud_directory = False
        self.omit_package_directories = False
        self.nextcloud_stat_directory = False
        self.cloud_parent_missing = False

    def call(self, endpoint, payload=None, **kwargs):
        payload = payload or {}
        if endpoint == "operations/stat":
            return {"item": {"IsDir": self.nextcloud_stat_directory if payload.get("fs", "").startswith("nextcloud_") else True}}
        if endpoint == "operations/list" and payload.get("fs", "").startswith("icloud_"):
            if payload.get("remote") and self.cloud_parent_missing:
                raise BridgeError("directory not found", 502)
            return {"list": [{"Path": "Nested/Report.pages", "IsDir": self.cloud_directory}]}
        if endpoint == "operations/list" and payload.get("opt", {}).get("recurse"):
            if payload["opt"].get("dirsOnly"):
                return {"list": [{"Path": "Nested/Report.pages", "IsDir": True}]}
            prefix = payload["remote"] + "/" if payload["remote"] else ""
            entries = [{"Path": path[len("Documents/"):], "IsDir": False, "Size": len(data),
                "ModTime": "2026-10-05T10:00:00Z"} for path, data in self.contents.items()
                if path.startswith("Documents/" + prefix)]
            if not payload["remote"] and not self.omit_package_directories:
                entries.append({"Path": "Nested/Report.pages", "IsDir": True})
            return {"list": entries}
        return super().call(endpoint, payload, **kwargs)


class IWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contents = {"Documents/Nested/Report.pages/Index.zip": b"document index",
                         "Documents/Nested/Report.pages/Data/photo ü.png": b"photo content",
                         "Documents/Nested/Report.pages/Metadata/Properties.plist": b"metadata"}
        self.store = Store(self.root, Fernet.generate_key().decode())
        self.store.save_user("alice", {"icloud_connected": True, "nextcloud_connected": True}, {})
        self.rc = PackageRC(self.contents)
        self.engine = Engine(self.store, self.rc, self.root, "https://cloud.example.invalid")
        self.job = self.engine.save_job("alice", {"icloud_path": "Documents", "nextcloud_path": "Documents", "mode": "upload"})
        self.run = {"id": "a" * 32, "uid": "alice", "job_id": self.job["id"], "job_name": "Documents",
                    "status": "running", "action": "run", "stats": {}, "log": "", "started": "2026-10-05T10:00:00Z", "progress": {}}
        self.store.put("runs", "alice", self.run)
        self.dav = MemoryDAV(self.contents)

    def tearDown(self):
        self.store.db.close(); self.temp.cleanup()

    def normalizer(self):
        item = IWork(self.engine, "alice", self.job, self.run, self.engine.refs("alice", self.job))
        item.plan()
        return item

    def test_complete_document_and_original_contents_are_preserved(self):
        item = self.normalizer()
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav):
            item.normalize()
        original = "Documents/Nested/Report.pages"
        self.assertFalse(self.dav.stat(original)["directory"])
        with zipfile.ZipFile(io.BytesIO(self.dav.files[original])) as document:
            for path, data in self.contents.items():
                self.assertEqual(document.read(path[len(original) + 1:]), data)
            self.assertIsNone(document.testzip())
        backup = self.run["iwork"]["backup_root"] + "/Nested/Report.pages"
        for path, data in self.contents.items():
            self.assertEqual(self.dav.files[backup + path[len(original):]], data)
        self.assertEqual(self.run["iwork"]["completed"], 1)
        self.assertNotIn("pending", self.run["iwork"])
        self.assertIsNone(self.run["progress"]["percent"])

    def test_preview_does_not_write_or_rename_documents(self):
        self.run["action"] = "preview"
        item = self.normalizer(); item.normalize()
        self.assertEqual(item.preview_filters(), ["/Nested/Report.pages", "/Nested/Report.pages/**"])
        self.assertEqual(self.dav.files, self.contents)
        self.assertEqual(self.run["iwork"]["planned"], 1)
        self.assertEqual(self.run["iwork"]["completed"], 0)

    def test_failed_promotion_restores_original_package(self):
        item = self.normalizer(); self.dav.fail_promotion = True
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav), self.assertRaises(BridgeError):
            item.normalize()
        for path, data in self.contents.items(): self.assertEqual(self.dav.files[path], data)
        self.assertTrue(self.dav.stat("Documents/Nested/Report.pages")["directory"])
        self.assertNotIn("pending", self.run["iwork"])

    def test_corrupt_upload_never_moves_original(self):
        item = self.normalizer(); self.dav.bad_upload = True
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav), self.assertRaises(BridgeError):
            item.normalize()
        self.assertEqual(self.dav.moves, [])
        for path, data in self.contents.items(): self.assertEqual(self.dav.files[path], data)

    def test_cancellation_before_copy_keeps_original(self):
        item = self.normalizer(); self.engine.cancel("alice", self.run["id"])
        with self.assertRaises(BridgeError): item.normalize()
        self.assertEqual(self.dav.moves, []); self.assertEqual(self.dav.writes, [])
        self.assertEqual(self.dav.files, self.contents)

    def test_cancellation_during_upload_keeps_original(self):
        item = self.normalizer()
        original_request = self.dav.request
        def request(method, *args, **kwargs):
            if method == "PUT": self.engine.cancel("alice", self.run["id"])
            return original_request(method, *args, **kwargs)
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav), patch.object(self.dav, "request", side_effect=request), self.assertRaises(BridgeError):
            item.normalize()
        self.assertEqual(self.dav.moves, [])
        for path, data in self.contents.items(): self.assertEqual(self.dav.files[path], data)

    def test_changed_source_keeps_original_and_does_not_promote_copy(self):
        item = self.normalizer()
        original_request = self.dav.request
        def request(method, *args, **kwargs):
            result = original_request(method, *args, **kwargs)
            if method == "PUT": self.dav.revision = '"changed"'
            return result
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav), patch.object(self.dav, "request", side_effect=request), self.assertRaises(BridgeError):
            item.normalize()
        self.assertEqual(self.dav.moves, [])
        for path, data in self.contents.items(): self.assertEqual(self.dav.files[path], data)

    def test_duplicate_and_outside_package_paths_are_rejected(self):
        item = self.normalizer()
        entry = {"Path": "Nested/Report.pages/Index.zip", "IsDir": False, "Size": len(self.contents["Documents/Nested/Report.pages/Index.zip"])}
        for entries in ([entry, entry], [{**entry, "Path": "Nested/Report.pages/../other.txt"}], [{**entry, "Path": "other/Index.zip"}]):
            with self.subTest(entries=entries), self.assertRaises(BridgeError):
                item.pack(self.dav, "Documents/Nested/Report.pages", entries, self.root / "copy.zip", "Nested/Report.pages")
        self.assertEqual(self.dav.moves, []); self.assertEqual(self.dav.writes, [])

    def test_change_during_directory_move_restores_latest_original(self):
        item = self.normalizer()
        original_move = self.dav.move
        updated = b"a concurrent change that must never be replaced by the earlier copy"
        def move(source, destination, *args, **kwargs):
            original_move(source, destination, *args, **kwargs)
            if source == "Documents/Nested/Report.pages":
                self.dav.files[destination + "/Index.zip"] = updated
                self.dav.revision = '"changed-during-move"'
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav), patch.object(self.dav, "move", side_effect=move), self.assertRaises(BridgeError):
            item.normalize()
        self.assertTrue(self.dav.stat("Documents/Nested/Report.pages")["directory"])
        self.assertEqual(self.dav.files["Documents/Nested/Report.pages/Index.zip"], updated)
        self.assertEqual(self.run["iwork"]["completed"], 0)
        self.assertNotIn("pending", self.run["iwork"])

    def test_restart_recovers_interrupted_directory_rename(self):
        item = self.normalizer()
        original = "Documents/Nested/Report.pages"
        backup = self.run["iwork"]["backup_root"] + "/Nested/Report.pages"
        self.dav.mkdir_parents(backup); self.dav.move(original, backup, directory=True)
        self.run["iwork"]["pending"] = {"original": original, "backup": backup, "staged": "incoming.pages"}
        self.store.put("runs", "alice", self.run)
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav):
            restarted = Engine(self.store, self.rc, self.root, "https://cloud.example.invalid")
        for path, data in self.contents.items(): self.assertEqual(self.dav.files[path], data)
        saved = self.store.get("runs", "alice", self.run["id"])
        self.assertEqual(saved["status"], "interrupted")
        self.assertNotIn("pending", saved["iwork"])

    def test_directory_on_both_sides_is_copied_normally(self):
        self.rc.cloud_directory = True
        self.assertEqual(self.normalizer().documents, [])
        self.assertNotIn("iwork", self.run)

    def test_next_preview_recovers_failed_rename_before_reading_folders(self):
        item = self.normalizer()
        original = "Documents/Nested/Report.pages"
        backup = self.run["iwork"]["backup_root"] + "/Nested/Report.pages"
        self.dav.mkdir_parents(backup); self.dav.move(original, backup, directory=True)
        self.run.update(status="failed")
        self.run["iwork"]["pending"] = {"original": original, "backup": backup, "staged": "incoming.pages"}
        self.store.put("runs", "alice", self.run)
        current = {**self.run, "id": "b" * 32, "status": "running", "action": "preview", "progress": {}}
        current.pop("iwork")
        self.store.put("runs", "alice", current)
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav):
            self.engine.prepare("alice", self.job, current)
        for path, data in self.contents.items(): self.assertEqual(self.dav.files[path], data)
        self.assertNotIn("pending", self.store.get("runs", "alice", self.run["id"])["iwork"])
        self.assertEqual(current["iwork"]["planned"], 1)

    def test_package_option_is_enabled_for_existing_and_new_jobs(self):
        self.assertTrue(validate_job({"icloud_path": "A", "nextcloud_path": "B"})["iwork_packages"])
        with self.assertRaises(BridgeError): validate_job({"icloud_path": "A", "nextcloud_path": "B", "iwork_packages": "true"})
        self.job.pop("iwork_packages")
        self.assertEqual(self.normalizer().documents, ["Nested/Report.pages"])

    def test_file_ancestors_detect_packages_without_directory_rows(self):
        self.rc.omit_package_directories = True
        item = self.normalizer()
        self.assertEqual(item.documents, ["Nested/Report.pages"])
        self.assertEqual(self.run["preflight"]["packages_found"], 1)
        self.assertEqual(self.run["preflight"]["packages_to_prepare"], 1)

    def test_flat_documents_and_embedded_packages_are_not_converted_separately(self):
        entries = [{"Path": "Ordinary.pages", "IsDir": False},
                   {"Path": "Folder/Report.pages/Data/Embedded.numbers/Index.zip", "IsDir": False},
                   {"Path": "Folder/Report.pages/", "IsDir": True},
                   {"Path": "Costs.numbers/Index.zip", "IsDir": False},
                   {"Path": "Talk.KEY/Metadata", "IsDir": True}]
        self.assertEqual(package_paths(entries), ["Costs.numbers", "Talk.KEY", "Folder/Report.pages"])

    def test_parent_listing_overrides_misleading_cloud_stat_directory(self):
        # Fake NewObject/stat reports a directory, but the actual listing is a file.
        self.assertTrue(self.rc.call("operations/stat", {"fs": "icloud_test:", "remote": "Nested/Report.pages"})["item"]["IsDir"])
        self.assertEqual(self.normalizer().documents, ["Nested/Report.pages"])

    def test_new_cloud_parent_folder_does_not_block_complete_package_copy(self):
        self.rc.cloud_parent_missing = True
        self.assertEqual(self.normalizer().documents, ["Nested/Report.pages"])

    def test_disabled_package_handling_stops_conflict_before_any_transfer(self):
        self.job["iwork_packages"] = False
        self.store.put("jobs", "alice", self.job)
        with patch.object(self.rc, "start_transfer", wraps=self.rc.start_transfer) as start:
            self.engine.execute("alice", self.run)
        start.assert_not_called()
        saved = self.store.get("runs", "alice", self.run["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIn("disabled", saved["error"])
        self.assertIn("Nested/Report.pages", saved["error"])
        self.assertFalse(saved["preflight"]["iwork_enabled"])
        self.assertEqual(saved["progress"]["problem"]["path"], "Nested/Report.pages")
        self.assertIn("iWork package preflight", saved["log"])
        self.assertEqual(self.dav.files, self.contents)

    def test_verification_stops_document_still_reported_as_directory(self):
        self.rc.nextcloud_stat_directory = True
        self.store.put("jobs", "alice", self.job)
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav), patch.object(self.rc, "start_transfer", wraps=self.rc.start_transfer) as start:
            self.engine.execute("alice", self.run)
        start.assert_not_called()
        self.assertEqual(self.run["status"], "failed")
        self.assertIn("before rclone started", self.run["error"])
        self.assertEqual(self.run["progress"]["problem"]["path"], "Nested/Report.pages")
        self.assertFalse(self.dav.stat("Documents/Nested/Report.pages")["directory"])

    def test_package_preflight_log_survives_transfer_log_updates(self):
        with patch("bridge.iwork.NextcloudDAV.for_user", return_value=self.dav):
            self.engine.execute("alice", self.run)
        self.assertEqual(self.run["status"], "success")
        self.assertIn("iWork package preflight", self.run["log"])
        self.assertIn("iWork package preparation verified", self.run["log"])
        self.assertTrue(self.run["log"].endswith("dry run log"))
        self.assertNotIn("preflight_log", self.engine.public_run(self.run))
