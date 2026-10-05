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
