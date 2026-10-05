"""Optional real rclone integration tests; CI runs these inside the built worker image."""
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import base64
import os
import threading
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from xml.etree import ElementTree
from bridge.rclone import Rclone
from bridge.models import MARKER, make_command, validate_job
from bridge.engine import Engine
from bridge.dav import Dav
from bridge.store import Store
from bridge.server import handler_class
from cryptography.fernet import Fernet


@unittest.skipUnless(shutil.which("rclone"), "rclone binary not available")
class RcloneIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rc = Rclone(self.root, Fernet.generate_key().decode(), "integration-rc-password-" * 3)
    def tearDown(self):
        self.rc.close()
        self.temp.cleanup()
    def wait(self, transfer):
        for _ in range(300):
            code = transfer.poll()
            if code is not None:
                self.assertEqual(code, 0, transfer.log())
                return
            time.sleep(.1)
        transfer.cancel()
        self.fail("rclone transfer timed out")
    def test_encrypted_config_and_copy_flags(self):
        self.assertIn("RCLONE_ENCRYPT_V0", self.rc.config.read_text())
        src, dst = self.root / "src", self.root / "dst"
        src.mkdir(); dst.mkdir(); (src / "test.txt").write_text("source")
        self.wait(self.rc.start_transfer("copy", [str(src), str(dst)], {"dry-run": "true", "use-json-log": "true"}))
        self.assertFalse((dst / "test.txt").exists())
        self.wait(self.rc.start_transfer("copy", [str(src), str(dst)], {"use-json-log": "true"}))
        self.assertEqual((dst / "test.txt").read_text(), "source")
    def test_running_transfer_stops_via_authenticated_api(self):
        source, destination = self.root / "stop-source", self.root / "stop-destination"
        source.mkdir(); destination.mkdir()
        (source / "large.bin").write_bytes(b"x" * 1048576)
        (destination / "existing.txt").write_text("keep this file")
        store = Store(self.root, Fernet.generate_key().decode())
        store.save_user("alice", {"icloud_connected": True, "nextcloud_connected": True}, {})
        engine = Engine(store, self.rc, self.root, "https://cloud.example.invalid")
        engine.refs = lambda uid, job: {
            "icloud": str(source), "nextcloud": str(destination),
            "icloud_root": str(source) + "/", "nextcloud_root": str(destination) + "/",
        }
        job = engine.save_job("alice", {"name": "Stop test", "icloud_path": "Source", "nextcloud_path": "Destination",
                                        "mode": "download", "bandwidth": "16k", "backup": False})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(engine, "t" * 48))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        engine.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/"
        def request(endpoint, body, uid="alice"):
            import json
            req = Request(url + endpoint, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + "t" * 48,
                "X-Bridge-User": base64.b64encode(uid.encode()).decode(),
            })
            with urlopen(req, timeout=5) as response:
                return json.load(response)
        try:
            run = request("jobs/" + job["id"] + "/run", {"action": "run"})
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                active = store.get("runs", "alice", run["id"])
                if (active.get("stats") or {}).get("bytes", 0) and engine.transfer:
                    break
                time.sleep(.1)
            else:
                self.fail("The real rclone transfer did not start")
            self.assertEqual(active["progress"]["phase"], "transferring")
            self.assertGreater(active["progress"]["percent"], 0)
            self.assertLess(active["progress"]["percent"], 100)
            self.assertEqual(active["stats"]["totalBytes"], 1048576)
            self.assertTrue(active["stats"]["transferring"])
            process = engine.transfer.process
            with self.assertRaises(HTTPError) as error:
                request("runs/" + run["id"] + "/stop", {}, "bob")
            self.assertEqual(error.exception.code, 404)
            self.assertIsNone(process.poll())
            stopped = request("runs/" + run["id"] + "/stop", {})
            self.assertTrue(stopped["cancel_requested"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                stopped = store.get("runs", "alice", run["id"])
                if stopped["status"] == "stopped":
                    break
                time.sleep(.1)
            self.assertEqual(stopped["status"], "stopped", stopped.get("error"))
            self.assertIsNotNone(stopped["finished"])
            self.assertEqual(stopped["progress"]["phase"], "stopped")
            self.assertIsNone(stopped["progress"]["percent"])
            self.assertIsNotNone(process.poll())
            self.assertEqual((source / "large.bin").stat().st_size, 1048576)
            self.assertEqual((destination / "existing.txt").read_text(), "keep this file")
        finally:
            engine.stop_event.set()
            if engine.transfer:
                engine.transfer.finish_cancel(timeout=1)
            engine.runner_thread.join(timeout=5)
            server.shutdown(); server.server_close()
            store.db.close()
    def test_completed_and_unchanged_runs_retain_final_progress(self):
        source, destination = self.root / "progress-source", self.root / "progress-destination"
        source.mkdir(); destination.mkdir()
        (source / "file.bin").write_bytes(b"x" * 65536)
        store = Store(self.root, Fernet.generate_key().decode())
        store.save_user("alice", {"icloud_connected": True, "nextcloud_connected": True}, {})
        engine = Engine(store, self.rc, self.root, "https://cloud.example.invalid")
        engine.refs = lambda uid, job: {"icloud": str(source), "nextcloud": str(destination),
                                       "icloud_root": str(source) + "/", "nextcloud_root": str(destination) + "/"}
        job = engine.save_job("alice", {"icloud_path": "Source", "nextcloud_path": "Destination", "mode": "download", "backup": False})
        try:
            for expected_bytes in (65536, 0):
                run = engine.queue_run("alice", job["id"])
                engine.execute("alice", store.get("runs", "alice", run["id"]))
                completed = store.get("runs", "alice", run["id"])
                self.assertEqual(completed["status"], "success", completed.get("error"))
                self.assertEqual(completed["progress"]["percent"], 100)
                self.assertEqual(completed["progress"]["phase"], "success")
                self.assertEqual(completed["stats"]["bytes"], expected_bytes)
                self.assertEqual(completed["stats"]["transferring"], [])
                self.assertIsNone(completed["stats"]["eta"])
            self.assertEqual((destination / "file.bin").read_bytes(), (source / "file.bin").read_bytes())
        finally:
            store.db.close()
    def test_iwork_exclusions_preserve_file_directory_conflicts(self):
        source, destination, work = self.root / "packages-source", self.root / "packages-destination", self.root / "packages-work"
        source.mkdir(); destination.mkdir(); work.mkdir()
        (source / "Report.key").mkdir()
        (source / "Report.key" / "image.png").write_text("package content")
        (destination / "Report.key").write_text("original iCloud file")
        (source / "Report.pages").write_text("source document")
        (destination / "Report.pages").mkdir()
        (destination / "Report.pages" / "original.txt").write_text("existing directory")
        (source / "ordinary.txt").write_text("copy this file")
        excludes = ["*.pages", "*.pages/**", "*.numbers", "*.numbers/**", "*.key", "*.key/**"]
        (work / "filters.txt").write_text("".join("- " + pattern + "\n" for pattern in excludes))
        job = {**validate_job({"icloud_path": "Documents", "nextcloud_path": "Documents", "mode": "upload", "backup": False, "excludes": excludes}), "id": "job"}
        refs = {"icloud": str(destination), "nextcloud": str(source), "icloud_root": "unused:", "nextcloud_root": "unused:"}
        cmd, args, opts = make_command(job, refs, work, "packages", "run")
        self.wait(self.rc.start_transfer(cmd, args, opts))
        self.assertEqual((destination / "ordinary.txt").read_text(), "copy this file")
        self.assertEqual((destination / "Report.key").read_text(), "original iCloud file")
        self.assertEqual((source / "Report.key" / "image.png").read_text(), "package content")
        self.assertEqual((destination / "Report.pages" / "original.txt").read_text(), "existing directory")
        self.assertEqual((source / "Report.pages").read_text(), "source document")
    def test_readonly_webdav_gateway_with_real_rclone(self):
        source = self.root / "dav-source"
        source.mkdir()
        (source / "My file.txt").write_text("live content")
        store = Store(self.root, Fernet.generate_key().decode())
        store.save_user("alice", {"icloud_connected": True}, {"dav_password": "test-password"})
        engine = SimpleNamespace(store=store, rc=self.rc, root=self.root, remote=lambda uid, side: str(source))
        engine.dav = Dav(engine)
        mount = engine.dav.credentials("alice")
        auth = "Basic " + base64.b64encode((mount["username"] + ":" + mount["password"]).encode()).decode()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(engine, "t" * 48))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}" + mount["path"]
        try:
            req = Request(url, method="PROPFIND", headers={"Authorization": auth, "Depth": "1"})
            with urlopen(req) as response:
                self.assertEqual(response.status, 207)
                hrefs = [x.text for x in ElementTree.fromstring(response.read()).iter("{DAV:}href")]
            self.assertIn(mount["path"] + "My%20file.txt", hrefs)
            self.assertTrue(all(href.startswith(mount["path"]) for href in hrefs))
            with urlopen(Request(url + "My%20file.txt", headers={"Authorization": auth})) as response:
                self.assertEqual(response.read(), b"live content")
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url + "My%20file.txt", data=b"overwrite", method="PUT", headers={"Authorization": auth}))
            self.assertEqual(error.exception.code, 405)
            self.assertEqual((source / "My file.txt").read_text(), "live content")
        finally:
            server.shutdown(); server.server_close()
            engine.dav.close_user("alice")
            store.db.close()
    def test_bisync_initialization_then_change_and_deletion(self):
        a, b, work = self.root / "a", self.root / "b", self.root / "state"
        a.mkdir(); b.mkdir(); work.mkdir()
        marker = self.root / "marker.txt"
        marker.write_text("check")
        for folder in (a, b): shutil.copy2(marker, folder / MARKER)
        (a / "test.txt").write_text("source")
        (work / "filters.txt").write_text("+ " + MARKER + "\n")
        job = {**validate_job({"icloud_path":"a","nextcloud_path":"b","backup":False}),"id":"job","initialized":False}
        refs = {"icloud":str(a),"nextcloud":str(b),"icloud_root":"unused:","nextcloud_root":"unused:"}
        cmd, args, opts = make_command(job, refs, work, "run1", "initialize")
        self.wait(self.rc.start_transfer(cmd,args,opts))
        self.assertEqual((b / "test.txt").read_text(),"source")
        job["initialized"] = True
        (b / "new.txt").write_text("new on path2")
        cmd,args,opts=make_command(job,refs,work,"run2","run")
        self.wait(self.rc.start_transfer(cmd,args,opts))
        self.assertEqual((a / "new.txt").read_text(),"new on path2")
        # Permit one deletion in this intentionally small fixture.
        job["max_delete_percent"]=50
        (a / "test.txt").unlink()
        cmd,args,opts=make_command(job,refs,work,"run3","run")
        self.wait(self.rc.start_transfer(cmd,args,opts))
        self.assertFalse((b / "test.txt").exists())
    def test_bisync_conflicts_and_archived_deletions(self):
        a, b, work = self.root / "a", self.root / "b", self.root / "state"
        work.mkdir()
        for folder in (a, b): (folder / "Documents").mkdir(parents=True)
        marker = self.root / "marker.txt"
        marker.write_text("check")
        for folder in (a, b): shutil.copy2(marker, folder / "Documents" / MARKER)
        (a / "Documents" / "conflict.txt").write_text("initial cloud")
        (a / "Documents" / "delete.txt").write_text("keep in backup")
        (work / "filters.txt").write_text("+ " + MARKER + "\n")
        job = {**validate_job({"icloud_path":"Documents","nextcloud_path":"Documents","max_delete_percent":50}),"id":"job","initialized":False}
        refs = {"icloud":str(a / "Documents"),"nextcloud":str(b / "Documents"),"icloud_root":str(a)+"/","nextcloud_root":str(b)+"/"}
        cmd,args,opts=make_command(job,refs,work,"initial","initialize")
        self.wait(self.rc.start_transfer(cmd,args,opts))
        job["initialized"] = True
        stamp = time.time() + 10
        for folder, content, offset in ((a, "edited in iCloud", 0), (b, "edited in Nextcloud too", 10)):
            file = folder / "Documents" / "conflict.txt"
            file.write_text(content)
            os.utime(file, (stamp + offset, stamp + offset))
        cmd,args,opts=make_command(job,refs,work,"conflict","run")
        self.wait(self.rc.start_transfer(cmd,args,opts))
        for folder in (a, b):
            versions = {p.read_text() for p in (folder / "Documents").iterdir() if p.is_file()}
            self.assertIn("edited in iCloud", versions)
            self.assertIn("edited in Nextcloud too", versions)
        (a / "Documents" / "delete.txt").unlink()
        cmd,args,opts=make_command(job,refs,work,"deletion","run")
        self.wait(self.rc.start_transfer(cmd,args,opts))
        self.assertFalse((b / "Documents" / "delete.txt").exists())
        backups = list((b / "iCloud Bridge Backups" / "job" / "deletion").rglob("delete.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "keep in backup")
