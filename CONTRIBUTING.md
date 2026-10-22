# Contributing

Issues and pull requests are welcome. lineage is early, so opening an issue before a
large change saves us both time.

- **Setup:** Python 3.9+, stdlib only. For the checks:
  `python -m pip install pytest==8.3.3 pyflakes==3.2.0 croniter==6.0.0`, then
  `python -m pyflakes lineage tools tests`, `python -m pytest -q` and
  `python tools/conformance.py`. All three must pass; CI runs them on Linux and Windows.
- **Dependencies:** the package is stdlib-only and stays that way. Test tools are the
  exception, pinned in CI.
- **Read-only:** no code path may change anything on an endpoint. A PR that adds an
  action has to wait for signed policy and auth (ROADMAP step 2).
- **Tests:** a bug fix comes with a test that fails without it. Never commit real
  collected data, hostnames, IPs or user paths in a fixture or screenshot; use made-up
  names like the ones in `tools/demo.py`.
- **Conformance:** the baseline in `tests/conformance/baseline.json` only goes up.
- **Docs:** if a change makes something in `README.md` or `docs/` untrue, update it in
  the same PR.
- **Security issues:** see [SECURITY.md](SECURITY.md). Don't file them as public issues.

By contributing you agree your work is released under the [MIT License](LICENSE).
