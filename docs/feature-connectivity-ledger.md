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
  - No sandboxing beyond venv isolation — setup.py/build-hook arbitrary code execution risk is
    accepted per prior-art research, not yet mitigated
  - cache.py (SQLite failure-history cache) and cli.py (entry point) not yet built — this unit
    covers only the execution core
  - `python_version` parameter is ignored entirely by the pip fallback (stdlib `venv` always uses
    whichever interpreter is running the tool — cannot target an arbitrary Python version the way
    `uv venv --python X.Y` can). Not yet surfaced to the user as a limitation.
  - Only tested on Windows. macOS/Linux venv layout (`bin/` vs `Scripts/`) is handled in code
    (`_venv_python()` branches on `sys.platform`) but not exercised by CI — no Mac/Linux runner
    available in this environment.

## unit: runner.py — pip fallback backend (robustness pass)

- **Scope**: core
- **Trigger**: user pointed out the tool assumed `uv` is installed, which most machines won't have
- **Change**: extracted `_Backend` ABC (`create_venv`/`dry_run_install`/`parse`), added `_UvBackend`
  (renamed from the original monolithic logic) and `_PipBackend` (stdlib `venv` + `pip install
  --dry-run`). `_select_backend()` picks via `shutil.which("uv")`.
- **Evidence (Prove)**: `tests/test_pip_backend.py` (4 tests, forces `_PipBackend` explicitly) +
  `tests/test_backend_selection.py` (2 tests, mocks `shutil.which` to verify the fallback actually
  triggers when uv is absent — not just that the pip code path works when called directly)
- **Refutation (Refute)** — found via live run, not self-review:
  - pip's error stream differs by invocation: `python -m pip install` routes `ERROR:` lines to
    **stderr**, while an earlier manual test with a standalone `pip` executable had shown them on
    **stdout**. `_PipBackend._extract_failing_package` originally only checked stdout and silently
    returned `None` on every real failure — fixed to check both streams.
  - `probe_all()`'s failure-record line only stored `result.stderr or result.stdout` (whichever was
    non-empty first), which would drop half the pip error context when both streams have content —
    fixed to concatenate both.
- **Regress**: all 5 uv-backend tests + 4 pip-backend tests + 2 selection tests re-run together,
  11/11 pass, no cross-backend regression.
- **Connect**: both backends satisfy the same `_Backend` interface; `probe_all()`/`probe_once()`
  callers are backend-agnostic and unchanged at the call site.
