# Feature Connectivity Ledger

## unit: runner.py — dry-run probe with fail-fast retry loop

- **Scope**: core
- **Inputs**: list of pip requirement specs (e.g. `"numpy==0.0.1"`), target Python version
- **Outputs**: `probe_all()` → `{ok, failures: [{package, stderr}], resolved: [str]}`
- **State ownership**: none persisted yet — each call creates and destroys its own temp venv
  (`tempfile.TemporaryDirectory`), no shared state across calls (cache.py deferred to next unit)
- **External effects**: spawns `uv venv` + `uv pip install --dry-run` subprocesses; the dry-run
  step does query PyPI over the network (metadata resolution) but does not download/install/compile
- **Evidence (Prove)**: `tests/test_runner.py`, 5/5 passing (`python tests/test_runner.py`)
- **Refutation (Refute)**:
  - empty requirements list → ok, no crash
  - single unsatisfiable version → correctly attributed
  - two simultaneous failures (numpy + scipy) in one call → **initially only surfaced 1 of 2**
    (uv's resolver is fail-fast); fixed by iterative drop-and-retry loop in `probe_all()`
  - mutually conflicting constraints (`numpy>=2.0` + `numpy<1.20`) → correctly attributed to numpy
  - Windows cp949 encoding crash on uv's box-drawing error output (×, ╰, ▶) →
    **found via live run, not self-review** — fixed with `encoding="utf-8", errors="replace"`
    on both subprocess calls
  - uv's install-plan output ("+ package==version") — **found via live run**: goes to stderr,
    not stdout, contrary to initial assumption; `resolved_packages` parsing source corrected
- **Regress**: full suite re-run after both fixes, all 5 cases still pass, no new failures
- **Deferred risk**:
  - `probe_all` max_rounds=20 hard cap — untested against a requirements file with >20 simultaneous
    failures (unlikely in practice, but unverified)
  - No handling yet for uv itself being absent from PATH (assumes `uv` binary is installed)
  - No sandboxing beyond venv isolation — setup.py/build-hook arbitrary code execution risk is
    accepted per prior-art research, not yet mitigated
  - cache.py (SQLite failure-history cache) and cli.py (entry point) not yet built — this unit
    covers only the execution core
