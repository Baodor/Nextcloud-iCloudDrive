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
        selected = "Unterlagen/Unterlagen Test (Voll)"
        cloud_root = root / "cloud"
        cloud = cloud_root / selected; cloud.mkdir(parents=True)
        rc.call("config/create", {"name": "icloud_" + tenant(uid), "type": "alias", "parameters": {"remote": str(cloud_root)}})
        engine = Engine(store, rc, root, url)
        packages = {}
        for name in ("Nested/Report ü.pages", "Nested/Report ü.numbers", "Nested/Report ü.key", "Expenses.numbers", "New cloud folder/New.pages"):
            contents = {"Index.zip": b"opaque iWork document index", "Data/photo ü.png": b"original photo", "Metadata/Properties.plist": b"original metadata"}
            packages[name] = contents
            for child, data in contents.items():
                destination = selected + "/" + name + "/" + child
                client.mkdir_parents(destination)
                with client.request("PUT", destination, data):
                    pass
            client.mkdir_parents(selected + "/" + name + "/Empty/placeholder")
        client.mkdir_parents(selected + "/ordinary.txt")
        with client.request("PUT", selected + "/ordinary.txt", b"ordinary content"):
            pass
        flat = archive({"Index.zip": b"already a complete document"})
        with client.request("PUT", selected + "/Ordinary.pages", flat):
            pass
        for name in packages:
            if name.startswith("New cloud folder/"):
                continue
            (cloud / name).parent.mkdir(parents=True, exist_ok=True)
            (cloud / name).write_bytes(b"existing complete document")
        job = engine.save_job(uid, {"icloud_path": selected, "nextcloud_path": selected, "mode": "upload", "backup": False, "iwork_packages": False})

        def run(action, identifier=None):
            result = engine.queue_run(uid, identifier or job["id"], action)
            engine.execute(uid, store.get("runs", uid, result["id"]))
            saved = store.get("runs", uid, result["id"])
            assert saved["status"] == "success", (saved.get("error"), saved.get("log", "")[-4000:])
            assert saved["progress"]["percent"] == 100
            return saved

        blocked = engine.queue_run(uid, job["id"], "preview")
        engine.execute(uid, store.get("runs", uid, blocked["id"]))
        blocked = store.get("runs", uid, blocked["id"])
        assert blocked["status"] == "failed" and "disabled" in blocked["error"], blocked
        assert blocked["preflight"]["package_conflicts"] == 4
        assert not blocked.get("rc_job")
        job = engine.save_job(uid, {**job, "iwork_packages": True}, job["id"])
        preview = run("preview")
        assert preview["iwork"]["planned"] == 5 and preview["iwork"]["completed"] == 0
        assert preview["preflight"]["iwork_revision"] == 2
        for name in packages:
            assert client.stat(selected + "/" + name)["directory"]
        copied = run("run")
        assert copied["iwork"]["completed"] == 5
        assert "iWork package preparation verified" in copied["log"]
        for name, contents in packages.items():
            path = selected + "/" + name
            assert not client.stat(path)["directory"]
            with client.request("GET", path) as response:
                data = response.read()
            assert data == (cloud / name).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as document:
                assert document.testzip() is None
                assert "Empty/" in document.namelist()
                assert client.stat(copied["iwork"]["backup_root"] + "/" + name + "/Empty")["directory"]
                for child, expected in contents.items():
                    assert document.read(child) == expected
                    with client.request("GET", copied["iwork"]["backup_root"] + "/" + name + "/" + child) as response:
                        assert response.read() == expected
        assert (cloud / "ordinary.txt").read_bytes() == b"ordinary content"
        assert (cloud / "Ordinary.pages").read_bytes() == flat
        unchanged = run("run")
        assert not unchanged.get("iwork"), unchanged

        engine.delete_job(uid, job["id"])
        job = engine.save_job(uid, {"icloud_path": selected, "nextcloud_path": selected, "mode": "bisync", "initial": "nextcloud"})
        run("preview"); run("initialize")
        changed = archive({"Index.zip": b"iCloud change with a different size", "Data/photo.png": b"iCloud photo"})
        (cloud / "Nested" / "Report ü.pages").write_bytes(changed)
        run("run")
        with client.request("GET", selected + "/Nested/Report ü.pages") as response:
            assert response.read() == changed
        changed = archive({"Index.zip": b"Nextcloud change with another different size", "Data/photo.png": b"Nextcloud photo", "extra.txt": b"keep this too"})
        with client.request("PUT", selected + "/Nested/Report ü.pages", changed):
            pass
        run("run")
        assert (cloud / "Nested" / "Report ü.pages").read_bytes() == changed
        print("Real Nextcloud iWork preservation, read-only preview and both rclone sync directions passed")
    finally:
        rc.close()
        store.db.close()
