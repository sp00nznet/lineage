"""lineage.server — publish policy, accept reports, write opsview-shaped state.

The heritage payoff: reports land as

    <state>/state/<host_key>/<item_id>/latest.json

in exactly the wrapper shape opsview's `orchestrator/state.py` writes and its
Flask API already reads. That is not a coincidence and it should not drift --
it means opsview's console can render a lineage fleet with no new UI, and it is
why this server ships no UI. Discovery data has its own read-only console,
`lineage.web`.

ponytail: stdlib http.server, no Flask, no database. It publishes a JSON file
and appends to a directory tree. Swap in opsview's `web/api.py` when this needs
auth, TLS and a real console -- that is the upgrade path, and it is already
written.

SECURITY, stated plainly: there is no authentication here. The v1 agent takes
no actions, so the exposure is inventory poisoning and disclosure rather than
remote code execution -- but that stops being true the moment actions land.
Binds to 127.0.0.1 by default. Do not put this on a network until the mutual
auth in docs/api.md ("Not yet built") exists.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_BODY = 4 * 1024 * 1024      # a report from a sane policy is kilobytes
HISTORY_RETENTION = 50          # matches opsview


def _safe_key(raw):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(raw or "unknown"))[:120]


class Store:
    """Disk-backed, opsview-compatible. No database until one is needed."""

    def __init__(self, root):
        self.root = Path(root)
        self.state = self.root / "state"
        self.state.mkdir(parents=True, exist_ok=True)

    def policy_path(self):
        return self.root / "policy.json"

    def load_policy(self):
        p = self.policy_path()
        if not p.exists():
            return {"version": 0, "items": []}
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError as e:
            # A malformed policy must not take the fleet down: serve nothing,
            # agents keep their cached copy.
            print(f"[lineage] policy.json is invalid ({e}); serving an empty policy")
            return {"version": 0, "items": []}

    def write_report(self, report):
        host_key = _safe_key(report.get("hostname") or report.get("agent_id"))
        captured = int(report.get("sent_at") or time.time())
        host_block = {
            "name": report.get("hostname"),
            "ip": report.get("ip"),
            "vcenter": None,
            "agent_id": report.get("agent_id"),
        }

        for r in report.get("results", []):
            item_dir = self.state / host_key / _safe_key(r.get("id"))
            item_dir.mkdir(parents=True, exist_ok=True)
            error = r.get("error")
            wrapped = {
                "host": host_block,
                "probe": r.get("id"),
                "captured_at": captured,
                "result": {
                    "probe": r.get("id"),
                    "status": "fail" if error else "ok",
                    "summary": error or f"{r.get('kind', 'applicability')} = {r.get('value')}",
                    "data": {"kind": r.get("kind"), "value": r.get("value")},
                    "issues": [error] if error else [],
                    "_meta": {"duration_ms": r.get("duration_ms")},
                },
            }
            payload = json.dumps(wrapped, indent=2)
            (item_dir / "latest.json").write_text(payload)
            (item_dir / f"{captured}.json").write_text(payload)

            history = sorted(
                (p for p in item_dir.iterdir() if re.fullmatch(r"\d+\.json", p.name)),
                key=lambda p: p.stat().st_mtime,
            )
            for old in history[:-HISTORY_RETENTION]:
                try:
                    old.unlink()
                except OSError:
                    pass

        return host_key

    def inventory(self):
        """{host: {item: {value, error, captured_at}}} -- the whole fleet, flat."""
        out = {}
        if not self.state.exists():
            return out
        for host_dir in sorted(self.state.iterdir()):
            if not host_dir.is_dir():
                continue
            items = {}
            for item_dir in sorted(host_dir.iterdir()):
                latest = item_dir / "latest.json"
                if not item_dir.is_dir() or not latest.exists():
                    continue
                try:
                    w = json.loads(latest.read_text())
                except json.JSONDecodeError:
                    continue
                data = w.get("result", {}).get("data", {})
                items[item_dir.name] = {
                    "value": data.get("value"),
                    "kind": data.get("kind"),
                    "error": (w.get("result", {}).get("issues") or [None])[0],
                    "captured_at": w.get("captured_at"),
                }
            out[host_dir.name] = items
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = "lineage/0.1"
    store: Store = None        # set by serve()

    def _json(self, code, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/policy":
            return self._json(200, self.store.load_policy())
        if self.path == "/v1/inventory":
            return self._json(200, self.store.inventory())
        if self.path in ("/", "/v1/status"):
            inv = self.store.inventory()
            return self._json(200, {
                "protocol": PROTOCOL_VERSION,
                "hosts": len(inv),
                "policy_version": self.store.load_policy().get("version", 0),
            })
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/report":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
        try:
            report = json.loads(self.rfile.read(length).decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return self._json(400, {"error": f"invalid JSON: {e}"})
        if not isinstance(report, dict) or report.get("protocol") != PROTOCOL_VERSION:
            return self._json(400, {"error": f"protocol must be {PROTOCOL_VERSION}"})

        host_key = self.store.write_report(report)
        return self._json(200, {"ok": True, "host": host_key, "items": len(report.get("results", []))})

    def log_message(self, fmt, *args):
        print(f"[lineage] {self.address_string()} {fmt % args}")


def serve(root, host="127.0.0.1", port=8770):
    Handler.store = Store(root)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[lineage] serving {Path(root).resolve()} on http://{host}:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("[lineage] WARNING: bound off-loopback with no authentication. See docs/api.md.")
    httpd.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lineage-server")
    ap.add_argument("--root", default="./var", help="state + policy.json directory")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args(argv)
    Path(args.root).mkdir(parents=True, exist_ok=True)
    serve(args.root, args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
