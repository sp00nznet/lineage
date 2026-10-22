#!/bin/sh
# lineage quick start for Linux and macOS: sh setup.sh
# Safe to run again: every step that is already done is skipped. Runs exactly
# the commands in the README's "Step by step", so the two routes cannot drift.
cd "$(dirname "$0")" || exit 1
LOG="$(pwd)/setup.log"
echo "lineage setup $(date)" > "$LOG"
echo
echo " lineage quick start"
echo " -------------------"

fail() { echo " $1 Details: $LOG"; exit 1; }

# 1. Python 3.9 or newer.
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >>"$LOG" 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo " Python 3.9 or newer is needed and was not found."
  if command -v apt-get >/dev/null 2>&1; then CMD="sudo apt-get install -y python3"
  elif command -v dnf >/dev/null 2>&1; then CMD="sudo dnf install -y python3"
  elif command -v brew >/dev/null 2>&1; then CMD="brew install python@3.12"
  else fail "Install Python 3.9+ from https://www.python.org, then run setup.sh again."; fi
  echo " It can be installed now with: $CMD  (about 30 MB)"
  printf " Install it now? [y/N] "; read -r answer
  case "$answer" in y|Y) ;; *) echo " Setup stopped. Install Python 3.9+, then run setup.sh again."; exit 1;; esac
  $CMD >>"$LOG" 2>&1 || fail "Python could not be installed."
  PY=python3
fi
echo " [ok] $($PY --version 2>&1)"

# 2. Self-test. No test framework needed.
$PY tests/run.py >>"$LOG" 2>&1 || fail "The self-test failed, so nothing else was run."
echo " [ok] self-test passed"

# 3. The example policy becomes this machine's cached policy.
if [ ! -f var/agent-state/policy.json ]; then
  mkdir -p var/agent-state && cp var/policy.json var/agent-state/policy.json
fi
echo " [ok] example policy in var/agent-state"

# 4. First evaluation of this machine, offline: no server, nothing sent anywhere.
$PY -m lineage.agent --once --offline --state var/agent-state || fail "The first evaluation failed."

# 5. A launcher that does step 4 again and shows the answers.
if [ ! -f lineage.sh ]; then
  cat > lineage.sh <<EOF
#!/bin/sh
cd "\$(dirname "\$0")" || exit 1
$PY -m lineage.agent --once --offline --state var/agent-state
$PY -m json.tool var/agent-state/last-report.json
EOF
  chmod +x lineage.sh
fi
echo " [ok] lineage.sh created: run it to evaluate this machine again."
echo
echo " Done. The full answers are in var/agent-state/last-report.json."
echo " The example policy is written for Windows, so on Linux most items report an"
echo " error: a check that could not run, never a false answer."
