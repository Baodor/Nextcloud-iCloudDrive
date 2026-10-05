"""Private loopback-only rclone RC client. No RC endpoints are exposed to users."""
import base64
import json
import os
import re
import subprocess
import socket
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from .models import BridgeError


def redact(text, secrets=()):
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r'(?i)(password|trust_token|cookies|authorization)([\"\s:=]+)[^\s,}]+', r'\1\2[REDACTED]', text)
    return text[:100000]


class Rclone:
    def __init__(self, root, password, rc_password):
        self.root = root
        self.config = root / "rclone.conf"
        self.env = {**os.environ, "RCLONE_CONFIG": str(self.config), "RCLONE_CONFIG_PASS": password,
                    "RCLONE_RC_USER": "bridge", "RCLONE_RC_PASS": rc_password}
        self.auth = "Basic " + base64.b64encode(f"bridge:{rc_password}".encode()).decode()
        self.opener = build_opener(ProxyHandler({}))
        if not self.config.exists():
            self.config.touch(mode=0o600)
            helper = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config_password.py")
            result = subprocess.run(["rclone", "config", "encryption", "set", "--password-command", "python3 " + helper],
                                    env=self.env, capture_output=True, timeout=30)
            if result.returncode:
                raise RuntimeError("Cannot initialize encrypted rclone configuration.")
        self.config.chmod(0o600)
        self.process = subprocess.Popen(["rclone", "rcd", "--rc-addr", "127.0.0.1:5572", "--rc-enable-metrics=false"],
                                        env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError("rclone stopped during startup; verify the saved configuration key.")
            try:
                self.call("core/version", timeout=1)
                break
            except BridgeError:
                time.sleep(.1)
        else:
            raise RuntimeError("rclone did not become ready.")

    def call(self, endpoint, payload=None, timeout=60):
        request = Request("http://127.0.0.1:5572/" + endpoint,
                          data=json.dumps(payload or {}).encode(),
                          headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.loads(error.read(100000)).get("error", "rclone operation failed.")
            except (ValueError, UnicodeDecodeError):
                message = "rclone operation failed."
            raise BridgeError(redact(str(message)), 502)
        except (URLError, TimeoutError, OSError):
            raise BridgeError("The rclone service is unavailable or timed out.", 503)

    def command(self, command, args, options=None, async_=False, group=None):
        # RC's generic opt map adds boolean values as positional arguments.
        # Explicit --flag=value arguments work for both bool and string flags.
        flags = [f"--{k}={v}" for k, v in (options or {}).items()]
        data = {"command": command, "arg": args + flags, "opt": {}, "returnType": "COMBINED_OUTPUT"}
        if async_:
            data.update({"_async": True, "_group": group})
        return self.call("core/command", data)

    def start_transfer(self, command, args, options):
        return Transfer(self, command, args, options)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Transfer:
    """Own the CLI process so cancellation is graceful and progress is real."""
    def __init__(self, rc, command, args, options):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.rc = rc
        self.lines = []
        self.size = 0
        self.lock = threading.Lock()
        self.cancelled_at = None
        flags = [f"--{k}={v}" for k, v in options.items()]
        flags += ["--rc=true", f"--rc-addr=127.0.0.1:{self.port}", "--stats=2s", "--stats-log-level=NOTICE"]
        self.process = subprocess.Popen(["rclone", command, *args, *flags], env=rc.env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()

    def drain(self):
        for line in self.process.stdout:
            line = redact(line)
            with self.lock:
                self.lines.append(line)
                self.size += len(line)
                while self.size > 100000 and len(self.lines) > 1:
                    self.size -= len(self.lines.pop(0))
        self.process.stdout.close()

    def poll(self):
        result = self.process.poll()
        if result is not None:
            self.reader.join(timeout=2)
        elif self.cancelled_at and time.monotonic() - self.cancelled_at > 90:
            self.process.kill()
        return result

    def cancel(self):
        if self.cancelled_at is None and self.process.poll() is None:
            self.cancelled_at = time.monotonic()
            self.process.send_signal(signal.SIGINT)

    def finish_cancel(self, timeout=10):
        """Finish a removed user's process before erasing its local state."""
        self.cancel()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)

    def log(self):
        with self.lock:
            return "".join(self.lines)

    def stats(self):
        try:
            request = Request(f"http://127.0.0.1:{self.port}/core/stats", data=b"{}",
                              headers={"Authorization": self.rc.auth, "Content-Type": "application/json"})
            with self.rc.opener.open(request, timeout=1) as response:
                return json.load(response)
        except (OSError, URLError, ValueError):
            return {}
