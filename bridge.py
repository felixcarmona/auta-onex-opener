"""HTTP bridge that opens an Auta ONEX intercom door with the owner's own account.

It reproduces exactly what the Auta ONEX Wi-Fi app does when you tap "open" on an
on-demand ("view door") call — using your own MyOnex account and the cloud PBX — so
it never touches or reconfigures the physical monitor/panel:

  1) log in to the Onex API and get a JWT
  2) auto-discover your SIP account, the monitor and the street panel from the API
     (all overridable via env vars)
  3) PUT /monitor/{id}/plate_call {"PlateCall": n}   (arm the panel)
  4) register your SIP extension on the *real* PBX (ast-ssl.pro.auta.es) and place a
     call to sip:{monitor_ext}@pbx with an `X-PlateNumber` header; when the panel
     answers, send DTMF '1' (the open-door tone). The SIP part runs isolated in
     sip_open.py so a SIP failure can't take the service down.

Endpoints (listens on 127.0.0.1:8092 by default):
  GET  /health           -> {"ok": true}
  POST /open             -> fire-and-forget open (returns 202 immediately)
  POST /open?wait=1      -> open and wait for the result {"opened": bool, ...}
  POST /open?dry=1       -> only check that the call connects to the panel (does NOT open)
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("AUTA_API", "https://onex.auta.es:8443/api")
UA = os.environ.get("AUTA_USER_AGENT", "okhttp/4.12.0")

USER = os.environ["AUTA_USER"]
PASS = os.environ["AUTA_PASS"]
# The registrar the monitor/panel actually use. NOT the domain the API reports
# (that is the API host); discovered empirically to be Auta's production FreePBX.
PBX = os.environ.get("AUTA_PBX", "ast-ssl.pro.auta.es")
HOST = os.environ.get("AUTA_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("AUTA_BRIDGE_PORT", "8092"))

# Optional overrides; anything left blank is auto-discovered from the API.
OV_SIP_EXT = os.environ.get("AUTA_SIP_EXTENSION", "")
OV_SIP_PASS = os.environ.get("AUTA_SIP_PASSWORD", "")
OV_MON_ID = os.environ.get("AUTA_MONITOR_ID", "")
OV_MON_EXT = os.environ.get("AUTA_MONITOR_EXT", "")
OV_PLATE = os.environ.get("AUTA_PLATE_NUMBER", "")

_cache = {}
_token = None
_token_lock = threading.Lock()


def log(*a):
    print("[auta]", *a, flush=True)


def _req(path, data=None, method=None, token=None, ctype=None):
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    req.add_header("User-Agent", UA)
    if token:
        req.add_header("Authorization", "BEARER " + token)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def login():
    boundary = "----autabridge"
    body = b""
    for k, v in (("Email", USER), ("Password", PASS), ("FirebaseToken", "")):
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()
    d = _req("/login", body, "POST", ctype=f"multipart/form-data; boundary={boundary}")
    return d["response"]["Token"]


def get_token(force=False):
    """Cached JWT; (re-)logs in on first use or when forced."""
    global _token
    with _token_lock:
        if force or not _token:
            _token = login()
            log("logged in (token refreshed)" if force else "logged in")
        return _token


def api(path, data=None, method=None, ctype=None):
    """Authenticated request that re-logs in once and retries on an expired token (401/403)."""
    for attempt in range(2):
        try:
            return _req(path, data=data, method=method, token=get_token(force=attempt == 1), ctype=ctype)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            raise


def invalidate_discovery():
    _cache.clear()


def discover(force=False):
    """Fill in SIP account, monitor and panel from the API (cached). Env overrides win."""
    if _cache and not force:
        return _cache
    sip = api("/SIP")["response"]["SIP"]
    mon = api("/monitor")["response"]["Monitors"][0]
    plate = api("/plate")["response"]["Plates"][0]
    _cache.update({
        "sip_ext": OV_SIP_EXT or str(sip["Extension"]),
        "sip_pass": OV_SIP_PASS or sip["Password"],
        "monitor_id": OV_MON_ID or str(mon["MonitorID"]),
        "monitor_ext": OV_MON_EXT or str(mon["SIP"]["Extension"]),
        "plate": OV_PLATE or str(plate["Number"]),
    })
    log("discovered:", {k: (v if k != "sip_pass" else "***") for k, v in _cache.items()})
    return _cache


def plate_call(monitor_id, plate):
    body = json.dumps({"PlateCall": str(plate)}).encode()
    return api(f"/monitor/{monitor_id}/plate_call", body, "PUT", ctype="application/json")


def do_open(dry=False):
    t0 = time.time()
    cfg = discover()
    try:
        plate_call(cfg["monitor_id"], cfg["plate"])
    except urllib.error.HTTPError as e:
        # Stale discovery (monitor/panel changed)? Re-discover once and retry.
        if e.code in (400, 404, 409, 422):
            log("plate_call failed", e.code, "- re-discovering")
            invalidate_discovery()
            cfg = discover(force=True)
            plate_call(cfg["monitor_id"], cfg["plate"])
        else:
            raise
    env = dict(os.environ)
    env.update({
        "AUTA_SIP_EXTENSION": cfg["sip_ext"],
        "AUTA_SIP_PASSWORD": cfg["sip_pass"],
        "AUTA_SIP_DOMAIN": PBX,
        "TARGET": cfg["monitor_ext"],
        "PLATE": str(cfg["plate"]),
        "DO_REGISTER": "1",
        "SEND_DTMF": "0" if dry else "1",
    })
    def run_sip():
        p = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "sip_open.py")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        out = p.stdout + p.stderr
        return ("RESULT confirmed=True" in out), (not dry and "dtmf_sent=True" in out)

    connected, dtmf = run_sip()
    return {
        "ok": True,
        "connected": connected,
        "opened": None if dry else dtmf,
        "dry": dry,
        "took_s": round(time.time() - t0, 1),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path)
        if path.path != "/open":
            return self._send(404, {"error": "not found"})
        q = urllib.parse.parse_qs(path.query)
        dry = q.get("dry", ["0"])[0] == "1"
        wait = q.get("wait", ["0"])[0] == "1"
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            if dry or wait:
                res = do_open(dry=dry)
                log("open result:", res)
                self._send(200, res)
            else:
                # Default (button / HomeKit): fire in the background and answer at once,
                # so the switch does not stay "on" while the ~8s open runs.
                def _bg():
                    try:
                        log("open (bg) result:", do_open(dry=False))
                    except Exception as e:  # noqa: BLE001
                        log("open (bg) error:", repr(e))
                threading.Thread(target=_bg, daemon=True).start()
                self._send(202, {"ok": True, "started": True})
        except Exception as e:  # noqa: BLE001
            log("open error:", repr(e))
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *a):
        print("[auta]", self.address_string(), fmt % a, flush=True)


def warmup():
    """Prime the token + discovery cache at startup so the first open is fast and any
    misconfiguration shows up in the log immediately. Best-effort: a failure here is not
    fatal — the first /open retries lazily."""
    try:
        get_token()
        discover()
        log("warmup ok")
    except Exception as e:  # noqa: BLE001
        log("warmup failed (will retry on first open):", repr(e))


def main():
    threading.Thread(target=warmup, daemon=True).start()
    log(f"listening on http://{HOST}:{PORT} (pbx {PBX})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
