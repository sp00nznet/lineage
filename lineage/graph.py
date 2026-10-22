"""lineage.graph — D5: persist observations, form links across time, answer the question.

The constraint that shapes everything here: **a producer/consumer link is
time-separated.** A job writes at 02:15 and something reads at 03:00. A live
sweep of four containers found zero shared artifacts in one window, correctly --
nobody had the same file open twice at once. Links only exist once observations
accumulate, so the store is not a convenience, it is the mechanism.

The second constraint: **volume.** 48,000 observations in thirty seconds on one
node. Raw observations are never stored. They are resolved to storage identity
(D4), collapsed to a pattern (D3), and aggregated into one row per
`(namespace, actor, artifact, direction)` with a count and a time span. That is
what makes a thirteen-month window affordable rather than theoretical.

ponytail: sqlite3, which is stdlib. This is genuinely a counting-and-upsert
problem over a long window with time-range queries, so a JSON file would be the
wrong kind of lazy -- it would be rewritten in full on every merge.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .jobs import format_window
from .observe import BOTH, READ, WRITE, collapse_path, is_noise
from .paths import equivalence_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    host       TEXT NOT NULL,
    container  TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL,
    volume     TEXT NOT NULL,
    rel        TEXT NOT NULL,
    direction  TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    examples   TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (host, container, actor, volume, rel, direction)
);
CREATE INDEX IF NOT EXISTS edges_artifact ON edges (volume, rel);

-- When we were actually watching. Without this, "no activity" is a lie of
-- omission: a quiet artifact and an unwatched one look identical.
CREATE TABLE IF NOT EXISTS windows (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host    TEXT NOT NULL,
    tap     TEXT NOT NULL,
    started REAL NOT NULL,
    ended   REAL NOT NULL
);

-- Aliases for one machine: hostname, FQDN, every IP it answers on. Built from
-- what agents report about themselves, not hand-maintained.
CREATE TABLE IF NOT EXISTS host_aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
"""


def open_store(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Host identity -- what unblocks cross-host links
# --------------------------------------------------------------------------
def register_host(conn, canonical, aliases=()):
    """One machine, many names. `nfs://192.0.2.23` and `nfs://pve-a` are
    the same server, and until they collapse to one name no cross-host link can
    ever form -- the client names the server by IP and the server names itself
    by hostname."""
    rows = [(str(a).strip().lower(), canonical) for a in ([canonical] + list(aliases)) if a]
    conn.executemany("INSERT OR REPLACE INTO host_aliases (alias, canonical) VALUES (?, ?)", rows)
    conn.commit()


def host_map(conn):
    return {r["alias"]: r["canonical"] for r in conn.execute("SELECT alias, canonical FROM host_aliases")}


def canonical_volume(volume, aliases):
    """Rewrite the host part of a network volume to its canonical name."""
    for proto in ("nfs://", "smb://", "sshfs://", "ceph://"):
        if volume.startswith(proto):
            rest = volume[len(proto):]
            server, sep, tail = rest.partition("/")
            canon = aliases.get(server.lower())
            return f"{proto}{canon or server}{sep}{tail}"
    if volume.startswith("dev://"):
        rest = volume[len("dev://"):]
        server, sep, tail = rest.partition("/")
        canon = aliases.get(server.lower())
        return f"dev://{canon or server}{sep}{tail}"
    return volume


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------
def resolve_observations(observations, resolver, exports_by_host=None, aliases=None):
    """Observations -> (volume, rel-pattern) rows, ready to aggregate.

    `resolver(obs) -> VolumeRef` keeps this decoupled from how mount tables are
    obtained, which differs between a host sweep and a per-container one.
    """
    exports_by_host = exports_by_host or {}
    aliases = aliases or {}
    out = []
    for o in observations:
        if is_noise(o["path"]):
            continue
        ref = resolver(o)
        if ref is None or ref.volume.startswith("unknown://"):
            continue
        # Prefer the portable identity so a local write and a remote read land
        # on the same row rather than two rows that never meet.
        keys = equivalence_keys(ref, exports_by_host.get(ref.host, ()))
        network = sorted(k for k in keys if not k[0].startswith("dev://"))
        volume, rel = network[0] if network else sorted(keys)[0]
        out.append({
            "host": o["host"], "container": o.get("container") or "",
            "actor": o["actor"], "volume": canonical_volume(volume, aliases),
            "rel": collapse_path(rel), "direction": o["direction"],
            "ts": o["ts"], "example": o["path"],
        })
    return out


def merge(conn, rows):
    """Aggregate into the store. Idempotent per row, additive across runs."""
    if not rows:
        return 0
    agg = {}
    for r in rows:
        k = (r["host"], r["container"], r["actor"], r["volume"], r["rel"], r["direction"])
        a = agg.get(k)
        if a is None:
            agg[k] = {"count": 0, "first": r["ts"], "last": r["ts"], "ex": []}
            a = agg[k]
        a["count"] += 1
        a["first"] = min(a["first"], r["ts"])
        a["last"] = max(a["last"], r["ts"])
        if len(a["ex"]) < 3 and r["example"] not in a["ex"]:
            a["ex"].append(r["example"])

    with conn:
        for k, a in agg.items():
            conn.execute("""
                INSERT INTO edges (host, container, actor, volume, rel, direction,
                                   count, first_seen, last_seen, examples)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (host, container, actor, volume, rel, direction) DO UPDATE SET
                    count      = count + excluded.count,
                    first_seen = MIN(first_seen, excluded.first_seen),
                    last_seen  = MAX(last_seen,  excluded.last_seen),
                    examples   = CASE WHEN LENGTH(examples) <= 2
                                      THEN excluded.examples ELSE examples END
            """, (*k, a["count"], a["first"], a["last"], json.dumps(a["ex"])))
    return len(agg)


def merge_store(conn, path):
    """Fold another host's store into this one. Returns the hosts replaced.

    Each agent's store holds only what that host observed, cumulatively, so a
    pull *replaces* that host's rows rather than adding to them. Adding would
    double every count on every pull; replacing makes pulling hourly and
    pulling monthly give the same answer.
    """
    conn.execute("ATTACH DATABASE ? AS src", (str(path),))
    try:
        hosts = [r[0] for r in conn.execute(
            "SELECT host FROM src.windows UNION SELECT host FROM src.edges")]
        with conn:
            for h in hosts:
                conn.execute("DELETE FROM edges WHERE host = ?", (h,))
                conn.execute("DELETE FROM windows WHERE host = ?", (h,))
            conn.execute("INSERT INTO edges SELECT * FROM src.edges")
            conn.execute("INSERT INTO windows (host, tap, started, ended) "
                         "SELECT host, tap, started, ended FROM src.windows")
            conn.execute("INSERT OR REPLACE INTO host_aliases SELECT * FROM src.host_aliases")
    finally:
        conn.execute("DETACH DATABASE src")
    return hosts


def record_window(conn, host, tap, started, ended):
    with conn:
        conn.execute("INSERT INTO windows (host, tap, started, ended) VALUES (?,?,?,?)",
                     (host, tap, started, ended))


def observed_days(conn, host=None):
    """Total wall-clock span we have watched, in days. The denominator for
    every coverage claim, and the reason those claims can be honest."""
    q = "SELECT MIN(started) a, MAX(ended) b FROM windows"
    args = ()
    if host:
        q += " WHERE host = ?"
        args = (host,)
    row = conn.execute(q, args).fetchone()
    if not row or row["a"] is None:
        return 0.0
    return max(0.0, (row["b"] - row["a"]) / 86400.0)


# --------------------------------------------------------------------------
# Links -- now across time, which is the whole point of persisting
# --------------------------------------------------------------------------
def links(conn, min_observations=1):
    """Producer -> artifact -> consumer, from accumulated history.

    The join is on storage identity, never on the path string, and the two
    sides need never have been observed in the same window -- which is what
    makes a nightly write and a morning read into one link.
    """
    rows = conn.execute("""
        SELECT w.host wh, w.container wc, w.actor wa, w.count wn, w.last_seen wl,
               r.host rh, r.container rc, r.actor ra, r.count rn, r.last_seen rl,
               w.volume, w.rel
        FROM edges w
        JOIN edges r ON w.volume = r.volume AND w.rel = r.rel
        WHERE w.direction IN (?, ?) AND r.direction IN (?, ?)
          AND NOT (w.host = r.host AND w.container = r.container AND w.actor = r.actor)
          AND w.count >= ? AND r.count >= ?
    """, (WRITE, BOTH, READ, BOTH, min_observations, min_observations)).fetchall()

    out = []
    for x in rows:
        cross_host = x["wh"] != x["rh"]
        cross_ns = cross_host or x["wc"] != x["rc"]
        out.append({
            "artifact": f'{x["volume"]}{x["rel"]}',
            "producer": {"host": x["wh"], "container": x["wc"] or None, "actor": x["wa"],
                         "count": x["wn"], "last_seen": x["wl"]},
            "consumer": {"host": x["rh"], "container": x["rc"] or None, "actor": x["ra"],
                         "count": x["rn"], "last_seen": x["rl"]},
            "cross_host": cross_host,
            "cross_namespace": cross_ns,
            "observations": min(x["wn"], x["rn"]),
        })
    out.sort(key=lambda l: (not l["cross_host"], -l["observations"]))
    return out


def dependents(conn, host, container=None):
    """Who consumes what this namespace produces. "What breaks", in one query."""
    return [l for l in links(conn)
            if l["producer"]["host"] == host
            and (container is None or l["producer"]["container"] == container)]


def dependencies(conn, host, container=None):
    """What this namespace consumes that someone else produces."""
    return [l for l in links(conn)
            if l["consumer"]["host"] == host
            and (container is None or l["consumer"]["container"] == container)]


# --------------------------------------------------------------------------
# The scream test
# --------------------------------------------------------------------------
def summarize_dependents(links_):
    """Group by *who* would break, not by which file.

    The raw join yields one row per writer/reader pair, so a file touched by
    six samba daemons appears six times. That answers "which artifacts" when
    the question being asked is "who breaks", and it buries the actor -- the
    only field that distinguishes those six rows.
    """
    by_consumer = {}
    for l in links_:
        c = l["consumer"]
        key = (c["host"], c["container"] or "", c["actor"])
        entry = by_consumer.get(key)
        if entry is None:
            entry = by_consumer[key] = {
                "host": c["host"], "container": c["container"], "actor": c["actor"],
                "artifacts": set(), "observations": 0, "last_seen": c["last_seen"],
            }
        entry["artifacts"].add(l["artifact"])
        entry["observations"] += l["observations"]
        entry["last_seen"] = max(entry["last_seen"], c["last_seen"])
    out = []
    for e in by_consumer.values():
        arts = sorted(e["artifacts"])
        out.append({**e, "artifacts": arts, "artifact_count": len(arts)})
    out.sort(key=lambda e: (-e["artifact_count"], -e["observations"]))
    return out


def scream_test(conn, host, container=None, jobs=(), seasonal=None, now=None):
    """Blast-radius preview. Never turns anything off; never renders a verdict.

    Three tiers, deliberately never merged. Merging them is how a confident
    tool causes the outage it was bought to prevent: CONFIRMED is what we saw,
    DECLARED is what is scheduled but has not run inside our window, and
    UNKNOWN is what we cannot attribute at all. The moment this grows a
    "safe to decommission" badge, people stop reading the tiers -- and the
    tiers are the product.
    """
    now = time.time() if now is None else now
    window_days = observed_days(conn)

    confirmed = dependents(conn, host, container)

    target = {host, container} - {None}
    declared, unknown = [], []
    for j in jobs:
        gap = j.get("max_gap_days")
        touches = (j.get("host") in target or j.get("container") in target)
        if not touches:
            if j.get("host") in (None, "") and j.get("container") in (None, ""):
                unknown.append(j)
            continue
        if j.get("observations", 0) == 0:
            declared.append({
                "id": j.get("id"), "schedule": j.get("schedule"),
                "frequency": j.get("frequency"), "max_gap_days": gap,
                "why": (f"declared {j.get('frequency', 'unknown')}; window is "
                        f"{format_window(window_days)}, so it may simply not have run yet")
                if gap is None or gap > window_days else
                "declared, in-window, and never observed",
            })

    return {
        "target": {"host": host, "container": container},
        "observed_window_days": round(window_days, 4),
        "observed_window": format_window(window_days),
        "confirmed": confirmed,
        "consumers": summarize_dependents(confirmed),
        "declared_not_observed": declared,
        "seasonal": seasonal or {},
        "unknown_jobs": len(unknown),
        # No verdict field. On purpose. See the docstring.
        "coverage_note": (
            f"Based on {format_window(window_days)} of observation. "
            f"{len(declared)} scheduled job(s) touching this target have not been seen run. "
            "Absence of activity here is not evidence that nothing depends on it."
        ),
    }


def render_scream_test(report):
    """Plain text, tiers in fixed order, seasonal warning first because a quiet
    box in the third week of the month is the exact shape of the mistake."""
    t = report["target"]
    name = t["container"] or t["host"]
    out = [f"Scream test — {name}", ""]

    seasonal = report.get("seasonal") or {}
    if seasonal.get("warning"):
        out += ["  ⚠ SEASONAL", f"    {seasonal['warning']}", ""]

    consumers = report.get("consumers", [])
    out.append(f"  CONFIRMED   observed dependents{'':>22}{len(consumers)}")
    for c in consumers[:10]:
        where = c["container"] or c["host"]
        actor = str(c["actor"]).rsplit("/", 1)[-1][:26]
        out.append(f"    • {actor:<26} on {where:<12} reads {c['artifact_count']:>3} artifact(s)")
        for a in c["artifacts"][:2]:
            out.append(f"        {a[:66]}")
    if len(consumers) > 10:
        out.append(f"    … and {len(consumers) - 10} more")

    declared = report["declared_not_observed"]
    out += ["", f"  DECLARED    scheduled, not seen in window{'':>10}{len(declared)}"]
    for d in declared[:10]:
        out.append(f"    • {str(d['id'])[:34]:<34} {str(d['frequency']):<9} {d['why']}")
    if len(declared) > 10:
        out.append(f"    … and {len(declared) - 10} more")

    out += ["", f"  UNKNOWN     jobs we cannot attribute{'':>13}{report['unknown_jobs']}",
            "", f"  {report['coverage_note']}"]
    return "\n".join(out)
