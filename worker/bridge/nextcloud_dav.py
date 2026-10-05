"""Private WebDAV operations for complete iWork documents and guarded renames."""
import base64
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from xml.etree import ElementTree as ET
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .models import BridgeError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def reveal(value):
    # rclone fs/config/obscure: this key is public; the outer config is encrypted.
    key = bytes.fromhex("9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638")
    try:
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(data) < 16:
            raise ValueError()
        decoder = Cipher(algorithms.AES(key), modes.CTR(data[:16])).decryptor()
        return (decoder.update(data[16:]) + decoder.finalize()).decode()
    except (ValueError, UnicodeDecodeError):
        raise BridgeError("Cannot read the saved Nextcloud connection.", 503) from None


class NextcloudDAV:
    def __init__(self, url, username, password):
        self.base = url.rstrip("/") + "/"
        self.auth = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def for_user(cls, engine, uid):
        config = engine.rc.call("config/get", {"name": engine.remote(uid, "nextcloud")[:-1]})
        public, _ = engine.store.user(uid)
        expected = engine.nc_url + "/remote.php/dav/files/" + quote(public.get("nextcloud_username", uid), safe="") + "/"
        if config.get("type") != "webdav" or config.get("url", "").rstrip("/") + "/" != expected:
            raise BridgeError("The saved Nextcloud connection does not match this account.", 409)
        return cls(expected, config.get("user", ""), reveal(config.get("pass", "")))

    def url(self, path):
        if path.startswith("/") or any(p in {".", ".."} for p in path.split("/")):
            raise BridgeError("Invalid document path.")
        return self.base + quote(path, safe="/")

    def request(self, method, path, data=None, headers=None):
        request = Request(self.url(path), data=data, method=method,
                          headers={"Authorization": self.auth, **(headers or {})})
        try:
            return self.opener.open(request, timeout=30)
        except HTTPError as error:
            error.close()
            raise BridgeError(f"Nextcloud returned HTTP {error.code} during document {method}.", error.code) from None
        except (URLError, TimeoutError, OSError):
            raise BridgeError("Nextcloud document access timed out or is unavailable.", 503) from None

    def stat(self, path):
        body = b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getetag/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>'
        try:
            with self.request("PROPFIND", path, body, {"Depth": "0", "Content-Type": "application/xml"}) as response:
                raw = response.read(1048577)
        except BridgeError as error:
            if error.status == 404:
                return None
            raise
        if len(raw) > 1048576:
            raise BridgeError("Nextcloud document metadata is too large.", 502)
        try:
            tree = ET.fromstring(raw)
            prop = next(p.find("{DAV:}prop") for p in tree.findall(".//{DAV:}propstat")
                        if " 200 " in (p.findtext("{DAV:}status") or ""))
            modified = prop.findtext("{DAV:}getlastmodified")
            return {"directory": prop.find("{DAV:}resourcetype/{DAV:}collection") is not None,
                    "etag": prop.findtext("{DAV:}getetag"),
                    "size": int(prop.findtext("{DAV:}getcontentlength") or 0),
                    "mtime": int(parsedate_to_datetime(modified).timestamp()) if modified else None}
        except (ET.ParseError, StopIteration, ValueError, TypeError):
            raise BridgeError("Nextcloud returned invalid document metadata.", 502) from None

    def mkdir_parents(self, path):
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            current = "/".join(parts[:i])
            try:
                with self.request("MKCOL", current):
                    pass
            except BridgeError as error:
                if error.status != 405 or not (self.stat(current) or {}).get("directory"):
                    raise

    def move(self, source, destination, etag=None, directory=False):
        headers = {"Destination": self.url(destination + ("/" if directory else "")), "Overwrite": "F"}
        if etag:
            headers["If-Match"] = etag
        with self.request("MOVE", source + ("/" if directory else ""), headers=headers):
            pass
