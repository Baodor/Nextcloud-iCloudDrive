"""Real Nextcloud WebDAV and rclone: complete iWork copies and both sync directions."""
from pathlib import Path
import io
import os
import tempfile
import zipfile
from cryptography.fernet import Fernet
from bridge.engine import Engine
from bridge.models import tenant
from bridge.nextcloud_dav import NextcloudDAV
from bridge.rclone import Rclone
from bridge.store import Store


def archive(contents):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as document:
        for path, data in contents.items():
            document.writestr(path, data)
    return result.getvalue()


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    uid, password, url = os.environ["TEST_NC_USER"], os.environ["TEST_NC_PASSWORD"], os.environ["NEXTCLOUD_URL"]
    key = Fernet.generate_key().decode()
    rc = Rclone(root, key, "temporary-iwork-rc-password-" * 3)
    store = Store(root, key)
    try:
        store.save_user(uid, {"icloud_connected": True, "nextcloud_connected": True, "nextcloud_username": uid}, {})
        client = NextcloudDAV(url + "/remote.php/dav/files/" + uid + "/", uid, password)
        rc.call("config/create", {"name": "nextcloud_" + tenant(uid), "type": "webdav",
            "parameters": {"url": client.base, "vendor": "nextcloud", "user": uid, "pass": password},
            "opt": {"obscure": True, "nonInteractive": True}})
        cloud = root / "cloud" / "Documents"; cloud.mkdir(parents=True)
        rc.call("config/create", {"name": "icloud_" + tenant(uid), "type": "alias", "parameters": {"remote": str(cloud.parent)}})
        engine = Engine(store, rc, root, url)
        packages = {}
        for extension in ("pages", "numbers", "key"):
            name = "Report ü." + extension
            contents = {"Index.zip": b"opaque iWork document index", "Data/photo ü.png": b"original photo", "Metadata/Properties.plist": b"original metadata"}
            packages[name] = contents
            for child, data in contents.items():
                destination = "Bridge iWork smoke/Nested/" + name + "/" + child
                client.mkdir_parents(destination)
                with client.request("PUT", destination, data):
                    pass
        client.mkdir_parents("Bridge iWork smoke/ordinary.txt")
        with client.request("PUT", "Bridge iWork smoke/ordinary.txt", b"ordinary content"):
            pass
        (cloud / "Nested").mkdir()
        for name in packages:
            (cloud / "Nested" / name).write_bytes(b"existing complete document")
        job = engine.save_job(uid, {"icloud_path": "Documents", "nextcloud_path": "Bridge iWork smoke", "mode": "upload", "backup": False})

        def run(action, identifier=None):
            result = engine.queue_run(uid, identifier or job["id"], action)
            engine.execute(uid, store.get("runs", uid, result["id"]))
            saved = store.get("runs", uid, result["id"])
            assert saved["status"] == "success", (saved.get("error"), saved.get("log", "")[-4000:])
            assert saved["progress"]["percent"] == 100
            return saved

        preview = run("preview")
        assert preview["iwork"]["planned"] == 3 and preview["iwork"]["completed"] == 0
        for name in packages:
            assert client.stat("Bridge iWork smoke/Nested/" + name)["directory"]
        copied = run("run")
        assert copied["iwork"]["completed"] == 3
        for name, contents in packages.items():
            path = "Bridge iWork smoke/Nested/" + name
            assert not client.stat(path)["directory"]
            with client.request("GET", path) as response:
                data = response.read()
            assert data == (cloud / "Nested" / name).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as document:
                assert document.testzip() is None
                for child, expected in contents.items():
                    assert document.read(child) == expected
                    with client.request("GET", copied["iwork"]["backup_root"] + "/Nested/" + name + "/" + child) as response:
                        assert response.read() == expected
        assert (cloud / "ordinary.txt").read_bytes() == b"ordinary content"
        unchanged = run("run")
        assert not unchanged.get("iwork"), unchanged

        engine.delete_job(uid, job["id"])
        job = engine.save_job(uid, {"icloud_path": "Documents", "nextcloud_path": "Bridge iWork smoke", "mode": "bisync", "initial": "nextcloud"})
        run("preview"); run("initialize")
        changed = archive({"Index.zip": b"iCloud change with a different size", "Data/photo.png": b"iCloud photo"})
        (cloud / "Nested" / "Report ü.pages").write_bytes(changed)
        run("run")
        with client.request("GET", "Bridge iWork smoke/Nested/Report ü.pages") as response:
            assert response.read() == changed
        changed = archive({"Index.zip": b"Nextcloud change with another different size", "Data/photo.png": b"Nextcloud photo", "extra.txt": b"keep this too"})
        with client.request("PUT", "Bridge iWork smoke/Nested/Report ü.pages", changed):
            pass
        run("run")
        assert (cloud / "Nested" / "Report ü.pages").read_bytes() == changed
        print("Real Nextcloud iWork preservation, read-only preview and both rclone sync directions passed")
    finally:
        rc.close()
        store.db.close()
