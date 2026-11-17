"""Shared password, signed session cookie, login throttle. Unchanged behaviour from the first web UI."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

SESSION_AGE = 7 * 24 * 3600
COOKIE = "plotter_session"


def pbkdf2(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200000).hex()


def load_web_config(path):
    """Load (or create) config/web.json (git-ignored). -> (cfg, plain password or None, source text)."""
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            cfg = {}
    changed = False
    for k in ("salt", "secret"):
        if not cfg.get(k):
            cfg[k] = secrets.token_hex(24)
            changed = True
    auth_env = os.environ.get("PLOTTER_WEB_AUTH", "").strip().lower()
    if auth_env in ("off", "0", "false", "no", "disabled"):
        cfg["auth"] = False
        changed = True
    elif auth_env in ("on", "1", "true", "yes", "enabled"):
        cfg["auth"] = True
        changed = True
    env_pw = os.environ.get("PLOTTER_WEB_PASSWORD")
    if env_pw:
        cfg["password_hash"] = pbkdf2(env_pw, cfg["salt"])
        changed, password, source = True, env_pw, "PLOTTER_WEB_PASSWORD"
    elif cfg.get("password_hash"):
        password, source = None, "stored hash"
    else:
        password = secrets.token_urlsafe(12)
        cfg["password_hash"] = pbkdf2(password, cfg["salt"])
        changed, source = True, "generated (first run)"
    cfg.setdefault("port", 8080)
    if changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return cfg, password, source


class Auth:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = cfg.get("auth", True)
        self.attempts = {}      # ip -> [fails, locked_until]
        self.lock = threading.Lock()

    def verify(self, password):
        return isinstance(password, str) and hmac.compare_digest(pbkdf2(password, self.cfg["salt"]),
                                                                 self.cfg["password_hash"])

    def make_session(self):
        ts = str(int(time.time()))
        sig = hmac.new(self.cfg["secret"].encode(), ts.encode(), hashlib.sha256).hexdigest()
        return f"{ts}.{sig}"

    def check_session(self, value):
        if not self.enabled:
            return True
        try:
            ts, sig = value.split(".", 1)
            expect = hmac.new(self.cfg["secret"].encode(), ts.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expect) and 0 <= time.time() - int(ts) <= SESSION_AGE
        except (ValueError, AttributeError):
            return False

    def login(self, ip, password):
        """-> (ok, error text, http code)"""
        now = time.time()
        with self.lock:
            rec = self.attempts.get(ip, [0, 0.0])
            if now < rec[1]:
                return False, "Too many wrong passwords. Wait a minute and try again.", 429
            if not self.verify(password):
                rec[0] += 1
                if rec[0] >= 5:
                    rec[:] = [0, now + 60]
                self.attempts[ip] = rec
                return False, "Wrong password.", 401
            self.attempts.pop(ip, None)
        return True, "", 200
