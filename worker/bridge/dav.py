"""Authenticated, read-only WebDAV gateway to isolated loopback rclone servers."""
import base64
import secrets
import socket
import subprocess
import threading
import time
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from .models import BridgeError, tenant, path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def rewrite_multistatus(body, prefix):
    """Keep DAV resource URLs within the tenant's public gateway path."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise BridgeError("The iCloud WebDAV service returned invalid XML.", 502) from exc
    for href in root.iter("{DAV:}href"):
        href.text = prefix + urlsplit(href.text or "").path.lstrip("/")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


class Dav:
    def __init__(self, engine, max_users=20):
        self.engine, self.max_users = engine, max_users
        self.lock = threading.RLock()
        self.sessions = {}
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def credentials(self, uid):
        public, secret = self.engine.store.user(uid)
        if not public.get("icloud_connected"):
            raise BridgeError("Connect iCloud first.", 409)
        return {"path": "/dav/" + tenant(uid) + "/", "username": tenant(uid),
                "password": secret["dav_password"], "readonly": True}

    def close_user(self, uid):
        with self.lock:
            session = self.sessions.pop(uid, None)
            if session:
                session["process"].terminate()
                try:
                    session["process"].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    session["process"].kill()

    def start_user(self, uid):
        with self.lock:
            for owner, session in list(self.sessions.items()):
                if session["process"].poll() is not None or (not session["active"] and time.monotonic() - session["used"] > 900):
                    self.close_user(owner)
            if uid not in self.sessions:
                if len(self.sessions) >= self.max_users:
                    raise BridgeError("The live browsing limit is reached. Retry after an idle session expires.", 503)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                password = secrets.token_urlsafe(32)
                env = {**self.engine.rc.env, "RCLONE_USER": "bridge", "RCLONE_PASS": password}
                process = subprocess.Popen(["rclone", "serve", "webdav", self.engine.remote(uid, "icloud"),
                    "--addr", f"127.0.0.1:{port}", "--read-only", "--vfs-cache-mode", "full",
                    "--cache-dir", str(self.engine.root / "cache" / tenant(uid)),
                    "--vfs-cache-max-size", "1G", "--vfs-cache-max-age", "1h", "--dir-cache-time", "1m",
                    "--poll-interval", "0"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                session = {"port": port, "process": process, "used": time.monotonic(), "active": 0,
                           "auth": "Basic " + base64.b64encode(f"bridge:{password}".encode()).decode()}
                self.sessions[uid] = session
                for _ in range(100):
                    if process.poll() is not None:
                        self.close_user(uid)
                        raise BridgeError("Live iCloud browsing could not start. Renew the Apple login if necessary.", 503)
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.05)
                else:
                    self.close_user(uid)
                    raise BridgeError("Live browsing timed out during startup.", 503)
            session = self.sessions[uid]
            session["used"] = time.monotonic()
            session["active"] += 1
            return session

    def proxy(self, handler):
        parts = urlsplit(handler.path).path.split("/", 3)
        if len(parts) < 4 or parts[1] != "dav":
            raise BridgeError("Unknown WebDAV endpoint.", 404)
        owner = next((uid for uid in self.engine.store.users() if tenant(uid) == parts[2]), None)
        if not owner:
            raise BridgeError("WebDAV authentication required.", 401)
        public, secret = self.engine.store.user(owner)
        expected = "Basic " + base64.b64encode(f"{tenant(owner)}:{secret.get('dav_password', '')}".encode()).decode()
        if not public.get("icloud_connected") or not secrets.compare_digest(handler.headers.get("Authorization", ""), expected):
            raise BridgeError("WebDAV authentication required.", 401)
        if handler.command not in {"OPTIONS", "PROPFIND", "GET", "HEAD"}:
            raise BridgeError("This iCloud mount is read-only. Edit the synchronized Nextcloud folders instead.", 405)
        raw_path = unquote(parts[3])
        normalized = path(raw_path)
        remote_path = quote(normalized, safe="/") + ("/" if raw_path.endswith("/") or not normalized else "")
        body = handler.read_body(1048576, json_=False)
        session = self.start_user(owner)
        response = None
        try:
            headers = {k: handler.headers[k] for k in ("Depth", "Range", "If-None-Match", "If-Modified-Since", "Content-Type", "Accept") if k in handler.headers}
            headers["Authorization"] = session["auth"]
            req = Request(f"http://127.0.0.1:{session['port']}/" + remote_path, data=body if body else None,
                          headers=headers, method=handler.command)
            try:
                response = self.opener.open(req, timeout=120)
            except HTTPError as e:
                response = e
            rewritten = None
            if handler.command == "PROPFIND" and response.status == 207:
                xml_body = response.read(16 * 1024 * 1024 + 1)
                if len(xml_body) > 16 * 1024 * 1024:
                    raise BridgeError("This folder listing is too large for live browsing.", 502)
                rewritten = rewrite_multistatus(xml_body, "/dav/" + tenant(owner) + "/")
            handler.send_response(response.status)
            for name in ("Content-Type", "Content-Length", "ETag", "Last-Modified", "Accept-Ranges", "Content-Range", "DAV", "Allow"):
                if name in response.headers:
                    if name != "Content-Length" or rewritten is None:
                        handler.send_header(name, response.headers[name])
            if rewritten is not None:
                handler.send_header("Content-Length", str(len(rewritten)))
            if "Location" in response.headers:
                suffix = urlsplit(response.headers["Location"]).path.lstrip("/")
                handler.send_header("Location", "/dav/" + tenant(owner) + "/" + suffix)
            handler.send_header("Connection", "close")
            handler.end_headers()
            if rewritten is not None:
                handler.wfile.write(rewritten)
            elif handler.command != "HEAD":
                while chunk := response.read(65536):
                    handler.wfile.write(chunk)
        except (URLError, TimeoutError, OSError):
            if response is None:
                raise BridgeError("Live iCloud browsing is unavailable.", 503)
        finally:
            if response:
                response.close()
            with self.lock:
                session["active"] -= 1
                session["used"] = time.monotonic()
