"""Run a bounded ETW file-activity capture and correlate it. Needs administrator.

    python tools/etw_capture.py --seconds 60

Read-only with respect to your files: it starts a trace session, reads it, and
deletes it. The session is bounded (circular, size-capped) and stopped on every
exit path including Ctrl-C, because a trace session that outlives its capture
keeps writing to disk forever.
"""
from __future__ import annotations

import argparse
import os
import platform
import socket
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.etw import (  # noqa: E402
    KEYWORDS, PROVIDER, correlate, decode, is_elevated, process_images,
    start_session, stop_session,
)
from lineage.observe import collapse_path, is_noise  # noqa: E402
from lineage.paths import nt_device_map  # noqa: E402

SESSION = "lineage-fileio"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bounded ETW file-activity capture.")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--max-mb", type=int, default=128)
    ap.add_argument("--keep-etl", action="store_true")
    ap.add_argument("--decode-only", metavar="ETL",
                    help="correlate an existing .etl and skip the capture. "
                         "Decoding needs no administrator, only capturing does.")
    args = ap.parse_args(argv)

    if platform.system() != "Windows":
        print("Windows only."); return 2

    started = time.time()

    if args.decode_only:
        etl = Path(args.decode_only)
        if not etl.exists():
            print(f"no such file: {etl}"); return 1
        print(f"decoding existing capture {etl} ({etl.stat().st_size / 1e6:.1f} MB)")
        args.keep_etl = True          # never delete a file we did not create
        images = process_images()     # best effort: the capture's processes may be gone
    else:
        if not is_elevated():
            print("Needs administrator: ETW kernel sessions cannot be started unelevated.")
            print("Re-run from an elevated prompt.")
            return 1

        etl = Path(tempfile.gettempdir()) / f"{SESSION}.etl"
        stop_session(SESSION)          # idempotent; clears a stale session

        print(f"provider {PROVIDER} keywords 0x{KEYWORDS:X} (READ excluded on purpose)"
              " + Kernel-Process for actor names")
        try:
            start_session(SESSION, str(etl), args.max_mb)
            images = process_images()
            print(f"capturing {args.seconds}s into {etl} (circular, max {args.max_mb} MB)…")
            time.sleep(args.seconds)
        except RuntimeError as e:
            print(f"could not start trace: {e}")
            return 1
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            # Every exit path. A session left running writes to disk indefinitely.
            stop_session(SESSION)
            print("session stopped")

    if not etl.exists():
        print("no trace file produced"); return 1
    print(f"etl size: {etl.stat().st_size / 1e6:.1f} MB — decoding…")

    devices = nt_device_map()
    print(f"drive map: {', '.join(f'{v}={k.rsplit(chr(92), 1)[-1]}' for k, v in sorted(devices.items(), key=lambda kv: kv[1]))}")

    corr = correlate(decode(str(etl)), socket.gethostname(), images, devices)
    obs = [o for o in corr.flush() if not is_noise(o["path"])]
    cov = corr.coverage()

    print()
    print(f"events decoded  : {cov['events_seen']}")
    print(f"observations    : {len(obs)}  (after noise filter)")
    print(f"unresolved      : {cov['unresolved_total']}  {cov['unresolved']}")
    print(f"evicted         : handles={cov['handles_evicted']} keys={cov['keys_evicted']}")
    print(f"coverage        : {cov['note']}")
    print()

    by_actor = Counter(os.path.basename(o["actor"]) for o in obs)
    print("--- busiest actors ---")
    for actor, n in by_actor.most_common(12):
        print(f"  {n:6}  {actor}")

    writes = Counter(collapse_path(o["path"]) for o in obs if o["direction"] == "write")
    print()
    print("--- most written artifacts (patterns) ---")
    for pat, n in writes.most_common(12):
        print(f"  {n:6}  {pat[:88]}")

    if not args.keep_etl:
        try:
            etl.unlink()
            print(f"\nremoved {etl}")
        except OSError:
            pass
    print(f"\nelapsed {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
