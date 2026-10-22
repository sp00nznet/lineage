"""lineage.web — a read-only console over collected discovery data.

    python -m lineage.web --fleet var/fleet          # http://127.0.0.1:8771

Reads what `tools/pve.py pull` (or `lineage.collect merge`) leaves behind:
`<fleet>/central.db` and `<fleet>/<host>/snapshots/*.json`. Writes nothing.

ponytail: stdlib http.server, one embedded page, vanilla JS. A store opened per
request, because sqlite connections do not cross threads. Swap for opsview's
console when this needs auth and many users.

SECURITY: no authentication, and the data names every file and program on the
watched hosts. Binds to 127.0.0.1; do not expose it.
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .graph import links, open_store, scream_test
from .jobs import format_window

GAP_S = 120           # a cycle's stop->start is seconds; more than this is a real gap
PAGE = Path(__file__).with_name("web.html")


def _name(actor):
    return os.path.basename(actor.replace("\\", "/")) or actor


def latest_jobs(fleet, host):
    snaps = sorted((Path(fleet) / host / "snapshots").glob("*.json"))
    return json.loads(snaps[-1].read_text()).get("jobs", []) if snaps else []


def overview(conn):
    hosts = {}
    for r in conn.execute("""SELECT host, tap, COUNT(*) n, MIN(started) a, MAX(ended) b,
                                    SUM(ended - started) watched FROM windows GROUP BY host, tap"""):
        hosts[r["host"]] = {"host": r["host"], "tap": r["tap"], "windows": r["n"], "first": r["a"],
                            "last": r["b"], "watched_s": r["watched"], "span_s": r["b"] - r["a"],
                            "watched": format_window(r["watched"] / 86400),
                            "span": format_window((r["b"] - r["a"]) / 86400)}
    for r in conn.execute("""SELECT host, COUNT(*) edge_rows, SUM(count) observations,
                                    COUNT(DISTINCT actor) actors, COUNT(DISTINCT volume || rel) artifacts
                             FROM edges GROUP BY host"""):
        hosts.setdefault(r["host"], {"host": r["host"]}).update(dict(r))
    ls = links(conn)
    return {"hosts": sorted(hosts.values(), key=lambda h: h["host"]),
            "links": len(ls), "cross_host_links": sum(l["cross_host"] for l in ls)}


def host_detail(conn, fleet, host):
    wins = [dict(r) for r in conn.execute(
        "SELECT started, ended FROM windows WHERE host = ? ORDER BY started", (host,))]
    gaps = [{"from": a["ended"], "to": b["started"], "seconds": b["started"] - a["ended"]}
            for a, b in zip(wins, wins[1:]) if b["started"] - a["ended"] > GAP_S]
    actors = [{**dict(r), "name": _name(r["actor"])} for r in conn.execute(
        """SELECT actor, SUM(count) observations, COUNT(*) artifacts,
                  SUM(CASE WHEN direction != 'read' THEN count ELSE 0 END) writes,
                  SUM(CASE WHEN direction != 'write' THEN count ELSE 0 END) reads,
                  MIN(first_seen) first_seen, MAX(last_seen) last_seen
           FROM edges WHERE host = ? GROUP BY actor ORDER BY observations DESC""", (host,))]
    jobs = latest_jobs(fleet, host)
    return {"host": host, "windows": wins, "gaps": gaps, "actors": actors, "jobs": jobs,
            "scream": scream_test(conn, host, jobs=jobs)}


def edges(conn, host=None, q="", limit=500):
    sql = "SELECT * FROM edges WHERE 1=1"
    args = []
    if host:
        sql += " AND host = ?"
        args.append(host)
    if q:
        sql += " AND (actor LIKE ? OR volume || rel LIKE ?)"
        args += [f"%{q}%"] * 2
    sql += " ORDER BY last_seen DESC LIMIT ?"
    args.append(min(int(limit), 5000))
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["examples"] = json.loads(d["examples"] or "[]")
        d["name"] = _name(d["actor"])
        out.append(d)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "lineage-web/0.1"
    fleet: Path = None

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, default=list).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        db = self.fleet / "central.db"
        if not db.exists():
            return self._json(404, {"error": f"no {db}; run tools/pve.py pull first"})
        conn = open_store(str(db))
        try:
            if u.path == "/api/overview":
                return self._json(200, overview(conn))
            if u.path.startswith("/api/host/"):
                return self._json(200, host_detail(conn, self.fleet, unquote(u.path[10:])))
            if u.path == "/api/edges":
                return self._json(200, edges(conn, qs.get("host"), qs.get("q", ""), qs.get("limit", 500)))
            if u.path == "/api/links":
                return self._json(200, links(conn))
            return self._json(404, {"error": "not found"})
        finally:
            conn.close()

    def log_message(self, fmt, *args):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lineage.web", description=__doc__.splitlines()[0])
    ap.add_argument("--fleet", default="var/fleet", help="directory holding central.db and <host>/snapshots")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8771)
    args = ap.parse_args(argv)
    Handler.fleet = Path(args.fleet)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[lineage] console on http://{args.host}:{args.port}  ({Handler.fleet.resolve()})")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[lineage] WARNING: off-loopback with no authentication; this lists every file on your hosts.")
    httpd.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
