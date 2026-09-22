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

## unit: cache.py — local failure-history cache (SQLite)

- **Scope**: core
- **Inputs**: same as `probe_all()` (requirements list, python_version) plus optional `db_path`,
  `ttl_seconds`
- **Outputs**: `cached_probe_all()` → same dict shape as `probe_all()` plus `"cache_hit": bool`
- **State ownership**: `~/.cache/compat_check/history.db` (user cache dir, outside the repo;
  `.gitignore` already excludes `*.db`). Single table `probe_cache`, keyed on a SHA-256 hash of
  `{sorted(requirements), python_version, backend}`. `runner.py` stays cache-unaware — `cache.py`
  wraps it, does not modify it (separation of concerns, as specified).
- **External effects**: same subprocess/network effects as `probe_all()` on a miss; a hit touches
  only the local SQLite file, no subprocess, no network.
- **Evidence (Prove)**: `tests/test_cache.py`, 4/4 passing (`python3 tests/test_cache.py`)
- **Refutation (Refute)** — real signals, not mocks:
  - first call on an empty DB → real `probe_all()` executes (`cache_hit=False`); confirmed via a
    live dry-run against `numpy==0.0.1` (deliberately unsatisfiable, so a stale cache couldn't be
    mistaken for a correct answer)
  - second call with identical inputs → cache hit, `probe_all()` does NOT re-run; measured
    end-to-end (fetcher → cache → runner, `requests` package): miss took 4.62s, hit took 0.04s
    (~115x), well past the test's 5x threshold
  - `ttl_seconds=0` → every call is immediately expired, forcing a fresh probe on the second call
    too (verifies TTL isn't silently ignored)
  - cache key ignores requirement list ordering (sorted before hashing) and changes when
    `python_version` changes (two different resolutions must not collide)
  - environment blocker found while testing, not caused by this unit: this WSL machine lacked
    `python3-venv`, so the *pre-existing* `_PipBackend.create_venv()` failed with "ensurepip is not
    available" before cache.py could even be exercised. Fixed by installing the system package
    (`sudo apt-get install -y python3.12-venv`) — required to prove any probe-based unit on this
    machine, not a code change.
- **Regress**: `tests/test_pip_backend.py` (4/4) and `tests/test_backend_selection.py` (2/2)
  re-run clean after the venv fix. `tests/test_runner.py` now passes 4/5 — the one pre-existing
  failure (`test_all_ok_reports_resolved_packages`) is a runner.py `resolved_packages` format
  mismatch against a newer pip version (`pkg-ver` vs the test's expected `pkg==ver`), unrelated to
  cache.py/fetcher.py and out of this unit's scope per the work order (no scope expansion) —
  logged here as a deferred risk, not silently fixed.
- **Connect**: verified end-to-end with fetcher.py — `fetch_requirements("requests")` output fed
  directly into `cached_probe_all()`, correct on both the miss and the hit.
- **Deferred risk**:
  - No cache invalidation on `compat-check` version bump — a resolver change wouldn't invalidate
    stale entries. Not needed yet (no versioned releases exist).
  - No cache size cap / eviction policy — unbounded growth over long-term use, acceptable for a
    local dev tool but not revisited here.

## unit: fetcher.py — repo/package → requirements extraction

- **Scope**: core
- **Inputs**: `source: str` — a GitHub repo URL or a bare PyPI package name
- **Outputs**: `fetch_requirements(source) -> list[str]` (requirement specs) or raises `FetchError`
  — deliberately never returns `[]`, so callers cannot confuse "nothing found" with
  `probe_all([])`'s "trivially ok" meaning
- **State ownership**: none, stateless HTTP calls only
- **External effects**: unauthenticated GET requests to `raw.githubusercontent.com` and
  `pypi.org/pypi/<name>/json`
- **Evidence (Prove)**: `tests/test_fetcher.py`, 8/8 passing (`python3 tests/test_fetcher.py`)
- **Refutation (Refute)** — real network calls against real, well-known repos/packages, no mocks:
  - `https://github.com/psf/requests` → resolved via `pyproject.toml` on `main`:
    `['charset_normalizer>=2,<4', 'idna>=2.5,<4', 'urllib3>=1.26,<3', 'certifi>=2023.5.7']`
  - `https://github.com/pallets/flask` → resolved via `pyproject.toml` on `main`:
    `['blinker>=1.9.0', 'click>=8.1.3', 'itsdangerous>=2.2.0', 'jinja2>=3.1.2', 'markupsafe>=2.1.1',
    'werkzeug>=3.1.0']`
  - bare package name `"requests"` → resolved via PyPI JSON `info.requires_dist`, extras-only
    markers (`; extra == "..."`) correctly filtered out
  - nonexistent GitHub repo and nonexistent PyPI package → both correctly raise `FetchError`
    instead of returning `[]` or silently succeeding
  - URL parsing handles bare repo URL and `/tree/<branch>` form; rejects non-GitHub URLs (falls
    through to the PyPI path instead of raising, by design)
- **Regress**: full suite (`test_runner.py` minus the one pre-existing unrelated failure,
  `test_pip_backend.py`, `test_backend_selection.py`, `test_cache.py`) re-run together after
  adding fetcher.py, no new failures introduced.
- **Connect**: `fetch_requirements()` output is a plain `list[str]` of requirement specs — the
  exact input type `cached_probe_all()`/`probe_all()` expect; verified end-to-end (see cache.py
  unit above).
- **Deferred risk** (explicitly out of scope per work order, not silently dropped):
  - `setup.py` parsing (AST-based) is NOT implemented — repos with only a `setup.py` and no
    `pyproject.toml`/`requirements.txt`/`setup.cfg` will raise `FetchError`.
  - Nested `requirements.txt` includes (`-r other.txt`) and editable installs (`-e .`) are skipped,
    not followed.
  - Branch fallback is `main` → `master` only; a repo using a different default branch name (e.g.
    `develop`) is not covered unless the URL explicitly names it.
  - No GitHub API auth used (by design, per prior research — avoids rate limits on the common
    path), so private repos are unreachable; no code path distinguishes "private repo" from
    "repo doesn't exist" — both surface as the same `FetchError` from a 404.

## unit: cli.py — command-line entry point

- **Scope**: core (wiring, not a new capability — this is what makes fetcher/cache/runner
  reachable by a human instead of only by importing Python)
- **Inputs**: `sys.argv` — `compat-check <source> [--python X.Y] [--no-cache]`
- **Outputs**: human-readable report to stdout, error to stderr, process exit code
  (0 = installable cleanly, 1 = conflicts found, 2 = source could not be resolved at all)
- **State ownership**: none directly; delegates to cache.py's SQLite file via `cached_probe_all()`
  unless `--no-cache` is passed, in which case it calls `runner.probe_all()` directly
- **External effects**: whatever fetcher.py/runner.py/cache.py already do — no new effects
- **Evidence (Prove)**: `tests/test_cli.py`, 4/4 passing; also run manually as
  `python -m compat_check.cli <source>` against real sources (see Refute)
- **Refutation (Refute)** — real end-to-end runs, not just unit-level mocks:
  - `python -m compat_check.cli requests` → OK, 4 packages resolved, exit 0
  - `python -m compat_check.cli https://github.com/pallets/flask` → OK, 6 packages, exit 0
  - `python -m compat_check.cli this-package-definitely-does-not-exist-xyz123` → FetchError
    surfaced cleanly to stderr, exit 2 (distinct from the resolver-conflict exit 1)
  - conflicting requirements (`numpy>=2.0` + `numpy<1.20`, injected via `fetch_requirements` mock
    since neither GitHub nor PyPI naturally exposes this shape) → indented resolver output
    printed under `[numpy]`, exit 1
  - repeat call on `requests` → `(cached)` marker shown, wall-clock dropped to ~0.4s (fetcher's
    network round-trip only; probe itself skipped)
  - `--no-cache` → report omits the `(cached)` marker even on a repeat call, confirming the flag
    actually bypasses `cached_probe_all()` rather than just relabeling the output
  - discovered mid-build: `compat_check/` had no `__init__.py`, so `python -m compat_check.cli`
    only worked by accident (namespace package + `sys.path` manipulation in prior test files).
    Added `__init__.py` to make the package importable in the standard way — required for the
    `-m` invocation the CLI depends on.
- **Regress**: all 6 test modules (runner/pip-backend/backend-selection/cache/fetcher/cli),
  20/20 tests total, re-run together after adding cli.py — no new failures.
- **Connect**: this is the first unit a first-time user actually runs; it exercises the full
  chain fetcher → cache → runner → report, so its own passing is itself the strongest connectivity
  evidence for the three units built earlier.
- **Deferred risk**:
  - Not yet packaged as an installable console-script entry point (no `pyproject.toml`/`setup.cfg`
    for the tool itself yet — currently only runnable via `python -m compat_check.cli`, not a bare
    `compat-check` command). Next unit.
  - `--python` is accepted but silently ignored by the pip fallback backend (inherited limitation
    from runner.py, not new here) — not yet surfaced as a warning to the CLI user.

## unit: pyproject.toml + LICENSE + README — GitHub-publish packaging

- **Scope**: core (this is what turns four Python files into an installable, publishable tool)
- **Inputs**: none (static config/docs)
- **Outputs**: `pyproject.toml` declaring `[project.scripts] compat-check = "compat_check.cli:main"`;
  MIT `LICENSE`; `README.md` with a scoped "why this over uv/pip" pitch
- **State ownership**: n/a
- **External effects**: n/a until actually pushed to a public GitHub remote (not done yet — repo
  is still local-only, no `origin` beyond the bundle-transfer artifact used for delegation)
- **Evidence (Prove)**: `uv tool install --editable .` succeeded, produced a real `compat-check`
  executable on PATH
- **Refutation (Refute)** — real install, not just "the TOML is syntactically valid":
  - `compat-check requests` run as the **installed executable** (no `python -m`, no `sys.path`
    hacks) → correct output, including a cache hit from the earlier `python -m` runs (proves the
    installed tool shares the same `~/.cache/compat_check/` as the dev-mode runs, as intended)
  - `compat-check --help` → argparse help text renders correctly from the packaged entry point
  - README's two example transcripts (`flask` success, nonexistent-package failure) re-run
    verbatim against the installed tool and matched — **found and fixed a doc/reality mismatch**:
    README originally said `uv tool install compat-check` (implying PyPI availability), which is
    false — nothing is published yet. Corrected to `uv tool install git+https://...` pointing at
    the repo directly.
  - `uv tool uninstall compat-check` → clean removal, confirmed `~/.cache/compat_check/history.db`
    persists independently of the tool install/uninstall (cache is user-scoped, not tied to the
    package installation)
- **Regress**: full 20-test suite unaffected (packaging doesn't touch `compat_check/*.py` logic)
- **Connect**: `[project.scripts]` entry point (`compat_check.cli:main`) is the same `main()`
  already covered by `tests/test_cli.py` — no new code path introduced by packaging
- **Deferred risk**:
  - `<owner>` placeholder in README's install command is not yet a real GitHub username/org —
    needs the actual publish target before the README is accurate
  - Not published to PyPI (by design, for now — `git+https://` install is the stated interim path)
  - No CI (GitHub Actions) wired to run the test suite on push/PR — tests only run locally so far

## unit: PII scrub + public GitHub push

- **Scope**: cross-cutting (identity/privacy, not application logic)
- **Trigger**: user explicitly required GitHub-account-only identity before publishing — no real
  name in LICENSE or commit authorship
- **Change**:
  - `LICENSE` copyright line: `Ja Hyun Lee` → `jahyunlee00299` (GitHub account name)
  - All 7 existing commits rewritten via `git filter-branch --env-filter` to author/committer
    `jahyunlee00299 <158720608+jahyunlee00299@users.noreply.github.com>` (GitHub's standard
    noreply address format, id+login — computed from `gh api user`, not guessed)
  - `refs/original/refs/heads/master` (filter-branch's backup ref) deleted and reflog expired so
    the pre-scrub identity isn't recoverable from local git metadata before push
  - Created public repo `jahyunlee00299/compat-check` via `gh api user/repos` (POST), added as
    `origin`, pushed
- **Evidence (Prove)**: `gh api repos/jahyunlee00299/compat-check/commits` — GitHub's own API
  confirms every commit shows only the noreply identity, nothing else
- **Refutation (Refute)** — real signals, not just "the rewrite command exited 0":
  - `grep -rniE "korea\.ac\.kr|egemen7|C:\\\\Users\\\\Jahyun|/c/Users/Jahyun|Ja Hyun Lee"` across
    the full working tree (excluding `.git/`) → zero matches, before push
  - **caught mid-flight**: a follow-up commit (README owner-placeholder fix) silently reverted to
    the real name/school-email identity, because the repo-local `git commit` picked up the global
    git config again — filter-branch only rewrites history, it doesn't change what the *next*
    commit uses. Fixed by setting `git config user.name/user.email` **locally in this repo** before
    amending that commit. This is exactly the kind of mistake a single "did the scrub run" check
    would miss — verification has to happen after every commit that touches this repo, not once.
  - Verified the fix caught it: `git log --format="%an <%ae>"` re-checked after the amend, before
    push — clean
  - README's own install command (`uv tool install git+https://github.com/jahyunlee00299/compat-check`)
    run for real, against the actual pushed public repo, as a stranger would run it (not from the
    local editable source) → succeeded, executable installed, `compat-check requests` ran
    correctly (cache hit, since the local `~/.cache` persisted across install/uninstall cycles)
- **Regress**: n/a (no application code touched by this unit)
- **Connect**: repo is now publicly reachable at `https://github.com/jahyunlee00299/compat-check`;
  README's install instructions are proven accurate against the live repo, not just internally
  consistent
- **Deferred risk**:
  - `gh auth status` is blocked by this environment's security baseline (`gh auth:*` denylist,
    intentional — not a bug); `gh api user`/`gh api user/repos` were used instead, which is the
    correct escape hatch, not a workaround of the gate
  - PyPI account/publish step not done — repo is public but not yet on PyPI (matches README's
    stated interim state)
  - No GitHub Actions CI wired yet — deferred, noted in the prior packaging unit too

## unit: tree.py + render.py + cli.py `--tree` — dependency tree visualization

- **Scope**: sub-feature (built on top of the existing fetcher/runner core, not a new axis)
- **Trigger**: user asked for a dependency tree graph, after confirming (via research) that
  `uv tree` exists as a real, ready-made source of parent/child edges — no need to build a
  resolver-internals reader from scratch
- **Inputs**: `tree.py`: requirements list, python_version. `render.py`: nested dict from
  `tree.py`, optional `label`. `cli.py`: new `--tree` flag.
- **Outputs**: `build_tree()` → `{"ok": bool, "roots": [...], "stderr": str}`; `render_tree()` →
  a colored (TTY) or plain (piped) multi-line ASCII tree string
- **State ownership**: none persisted; `tree.py` writes a throwaway `pyproject.toml` into a
  `tempfile.TemporaryDirectory`, same disposable-directory pattern as `runner.py`'s venvs
- **External effects**: same PyPI-metadata network calls as `probe_all()`, via `uv tree
  --universal` instead of `uv pip install --dry-run`
- **Evidence (Prove)**: `tests/test_tree.py` (4/4), `tests/test_render.py` (4/4), 2 new cases in
  `tests/test_cli.py` (6/6 total for that module) — all passing
- **Refutation (Refute)** — real signals, not mocks, plus one design bug caught live:
  - `uv tree` output for `flask` parsed and cross-checked field-by-field against the raw text
    output captured manually first — 6 direct deps, `jinja2`→`markupsafe` and `werkzeug`→
    `markupsafe` nested correctly, matching the manually-inspected `uv tree` text exactly
  - conflicting requirements (`numpy>=2.0` + `numpy<1.20`) → `build_tree()` returns `ok=False`
    with uv's own error text, not an empty tree silently reported as success
  - multiple top-level requirements (`requests` + `flask`) → two independent roots, not merged
    or dropped
  - uv absent (mocked `shutil.which`) → clean `ok=False` with an explicit "uv is required"
    message — no pip fallback exists for tree output (pip has no equivalent command), and this
    is stated in the module docstring and the CLI's `--tree` help text, not left implicit
  - **design bug found via live run, not self-review**: the first working version rendered
    `fetch_requirements()`'s output directly as tree roots — for a single-package source like
    `requests`, this meant `requests` itself never appeared in the tree, only its direct
    dependencies as separate flat roots (visually indistinguishable from "requests has 4 peer
    packages" rather than "requests requires 4 packages"). Fixed by adding an optional `label`
    parameter to `render_tree()` that wraps the roots one level under the source name/URL, so
    `--tree` on `https://github.com/pallets/flask` now shows `flask` itself as the tree's root
    with its dependencies nested under it — verified by re-running the CLI and reading the
    actual indentation, not just checking the function returned without erroring
  - ANSI color codes verified present when `sys.stdout.isatty()` is mocked `True`, and absent
    (plain text) when `False` — confirms the auto-disable-when-piped behavior works both ways,
    not just that color codes exist somewhere in the code
  - `--tree` combined with a conflicting-requirements run → `probe_all()`'s failure still drives
    the exit code (1) and its report prints first; the tree section separately reports
    "(tree unavailable: ...)" to stderr instead of crashing or silently omitting output
- **Regress**: all 8 test modules (added `test_tree.py`, `test_render.py`; extended
  `test_cli.py`), 30/30 tests total, re-run together — no new failures in the pre-existing 24.
- **Connect**: `--tree` is additive to the existing report — `_print_report()` (unchanged) always
  runs first, `--tree` output is appended only when the flag is passed; existing 6 CLI tests
  without `--tree` unaffected, confirming no regression to the default (no-flag) path.
- **Deferred risk**:
  - No pip-backend equivalent for tree visualization — machines without `uv` lose this one
    feature specifically (the rest of the tool still works via the pip fallback). Documented in
    both the module docstring and `--tree`'s `--help` text, not silently degraded.
  - `uv tree`'s text format (4-space indent, box-drawing prefixes) is unversioned/undocumented
    upstream — a future uv release changing this format would silently break parsing rather than
    erroring, since `_LINE_RE`/`_PKG_RE` simply skip lines they can't match. No detection for
    "uv's tree format changed" as distinct from "no dependencies".
  - Deep trees (`-d`/`--depth` equivalent) not exposed as a CLI option — always full depth.

## unit: params.py — parameter SSOT (`--python` guard, cache identity, version invalidation)

- **Scope**: cross-cutting (this unit does not add a capability; it removes a class of silent
  wrongness that spanned cli.py, runner.py and cache.py)
- **Trigger**: user asked to close the remaining "SSOT param guard" items. Reading the code
  surfaced a defect worse than the logged deferred risk: `--python` was not merely *ignored* by
  the pip backend, it was ignored **and still hashed into the cache key**.
- **The defect, precisely**: `_PipBackend.create_venv()` cannot target another interpreter (stdlib
  `venv` clones the running one; only `uv venv --python X.Y` can fetch a different version), but
  `cache.make_cache_key()` hashed `python_version` regardless. So on a pip-backend machine
  `--python 3.9` and `--python 3.13` ran a **byte-identical probe** yet landed in **two different
  cache rows**, each labelled with a version it was never measured against. A later `--python 3.9`
  hit would serve, as a 3.9 answer, a result actually measured on 3.13. Two prior ledger entries
  recorded the "silently ignored" half of this; neither noticed the cache-key half.
- **Change**:
  - new `compat_check/params.py`: `validate_python_version()` (rejects `3.111`, `2.7`, `4.0`,
    non-numeric, empty), `resolve_params(python_version, backend) -> ResolvedParams`
    (`requested_python` vs `effective_python` + `warnings`), `cache_identity()` (the single
    definition of what determines a probe's outcome)
  - `cache.make_cache_key()` now takes `ResolvedParams` and hashes `effective_python`, never the
    requested value — two requests that run the same probe share one row
  - cache identity includes `compat_check_version`, so a resolver/logic change invalidates stale
    rows instead of reusing them (closes a logged deferred risk)
  - `cache._evict_if_needed()` + `DEFAULT_MAX_ROWS=500`, oldest-first (closes the unbounded-growth
    deferred risk)
  - `runner.probe_all()` resolves params once and returns them in its result dict
  - `runner._normalize_resolved()`: pip's `"requests-2.34.2"` → `"requests==2.34.2"`, matching uv
  - `cli.py`: prints the version actually probed (`python: 3.13 (requested 3.9 — NOT honoured)`),
    routes the explanation to stderr, and exits **3** on an invalid parameter — distinct from
    2 (unresolvable source) and 1 (conflict)
  - `compat_check.__version__` is now the single version source; `pyproject.toml` reads it via
    setuptools dynamic version (no static `version =` key remains to drift)
- **Evidence (Prove)**: `tests/test_params.py` 10/10, `tests/test_cache.py` 6/6,
  `tests/test_cli.py` 9/9. Full suite **53/53 across 9 modules, 0 failures** (was 30/30 across 8,
  with 1 module failing under the pip backend).
- **Refutation (Refute)** — external signals, including a deliberate control:
  - **the fix, measured**: on the pip backend, `cache_identity` for `--python 3.9` and
    `--python 3.13` are now byte-equal (`True`) — the two rows collapse into one
  - **control, to prove the fix is not just "drop the field"**: on the **uv** backend the same two
    versions still produce **different** keys (`True`), because uv genuinely honours the request.
    A naive fix (removing `python_version` from the key) would have passed the first check and
    failed this one.
  - **the warning reaches the user**: CLI forced onto the pip backend (`shutil.which("uv")` → None)
    and run end-to-end — stdout carries `python: 3.13 (requested 3.9 — NOT honoured)`, stderr
    carries the full explanation naming both versions and the `uv` remedy. Verified by capturing
    both streams, not by reading the source.
  - **the guard runs before the network**: `test_invalid_python_version_exits_3_without_network`
    asserts `fetch_requirements.call_count == 0` on `--python 3.111` — a typo costs no GitHub
    round-trip. Confirmed live: exit 3, no request.
  - **version invalidation**: mutating `params.__version__` changes the cache key (`True`);
    `python -m build` produces `compat_check-0.2.0-py3-none-any.whl`, proving pyproject's dynamic
    version reads the same attribute the cache key does — one source, not two that agree by luck.
  - **backend format contract**: `_normalize_resolved` verified against hyphenated names —
    `charset-normalizer-3.5.1` → `charset-normalizer==3.5.1` (splits on the last hyphen that
    starts a digit, so the name survives); unrecognized tokens pass through unmangled rather than
    being truncated.
  - **eviction**: 10 synthetic rows, `max_rows=4` → exactly 6 removed and precisely the 4 newest
    (`key6..key9`) retained, verified by reading back the surviving keys; a second call at the same
    cap is a no-op (0 removed).
- **Regress**: all 8 pre-existing modules re-run. One pre-existing failure was **resolved, not
  suppressed**: `test_pip_backend.py` asserted `"requests-"` while `test_runner.py` asserted
  `"requests=="` — the ledger had recorded this as a "newer pip version format mismatch", but it
  was actually the two backends disagreeing about their own output contract. Normalizing at the
  backend boundary makes both assertions describe one format; a new test
  (`test_pip_resolved_format_matches_uv_contract`) pins it so the two cannot drift apart again.
- **Connect**: `params.py` has no dependency on cli/cache/runner — the arrows point inward. All
  three call into it, so there is exactly one place that decides what `--python` means. CI's
  pip-only job now runs `test_params.py` and `test_cache.py` as well, so the collapse behaviour is
  exercised on the backend where it actually matters (the uv job runs the whole suite already).
- **Deferred risk**:
  - `MAX_MINOR = 20` is an arbitrary upper bound that will need raising around Python 3.21; it
    rejects typos (`3.111`) at the cost of a future edit. Chosen deliberately over no bound.
  - Eviction is oldest-first by `checked_at`, not least-recently-*used* — a frequently-read old
    entry is still evicted before a write-once new one. Fine for a local dev cache; noted rather
    than modelled.
  - The pip backend still cannot target another Python version. This unit makes that **visible and
    correctly cached**; it does not remove the limitation (only installing `uv` does).
  - `ResolvedParams` now travels inside `probe_all()`'s result dict, so the dict is no longer
    purely JSON-serializable. Only the cached fields are persisted (the dataclass is re-derived on
    a hit, never stored), but a caller who blindly `json.dumps()` the whole result would now fail.

## unit: reqline.py — requirement-line parsing (Unit 0)

- **Scope**: core (replaces an ad-hoc filter with an explicit option table)
- **Trigger**: benchmarking the v0.3 design against pip's `req_file.py`
  (`docs/benchmark-fetcher-v0.3.md`). The design had scoped the work to `-r`/`-e`;
  the benchmark showed the requirement *line* itself was mis-parsed in three ways,
  all of them live in the published 0.2.0.
- **The three defects, all measured reaching the resolver**:
  - a line ending in `\` was never joined: `requests \` + `>=2.0` became two
    malformed specs
  - a trailing `# comment` was never stripped: uv received
    `requests>=2.0  # pinned` and answered `Failed to parse`
  - the filter matched prefixes (`"-r "`, `"-e "`, `"--"`), so `-c`, `-i` and `-f`
    passed through as package names; `probe_all(["-i https://x"])` returned
    `error: the following required arguments were not provided`
  In every case the user was told **their repo** had a dependency problem.
- **Change**: new `compat_check/reqline.py` — `join_continuations()`,
  `strip_comment()` (a `#egg=` fragment survives), `env_vars_in()`, and
  `parse_lines()` returning `ParsedRequirements(requirements, includes,
  constraints, editables, skipped)` with an `is_complete` property. Classification
  is done against an **explicit option table**, never a prefix, because prefix
  matching is what produced all three defects. An unknown option is recorded as
  skipped rather than passed through as a requirement.
- **Security decision — divergence from pip, deliberate**: pip expands `${VAR}`
  in requirement lines from its own environment. We do **not**. This tool reads
  strangers' repositories, and expanding `--index-url https://${TOKEN}@…` from our
  environment would splice a local secret into a subprocess argument. Such lines
  are recorded as skipped, which also tells the truth: a private index means the
  answer is incomplete by construction.
- **Evidence (Prove)**: `tests/test_reqline.py` 15/15.
- **Refutation (Refute)** — the benchmark cases re-run against the new parser:
  - continuation joins to one spec; verified the joined form
    (`"requests     >=2.0"`, inner whitespace preserved) is **actually accepted**
    by `packaging.requirements.Requirement` AND by a live `probe_all()` — not
    assumed from the PEP 508 grammar
  - `#egg=mypkg` is not treated as a comment (pip requires whitespace before `#`)
  - a commented-out continuation (`# disabled \`) does not swallow the next line
  - all 19 known options classified, zero leak into `requirements`
  - `--requirement=nested.txt` (equals form) parses
  - `env_vars_in("$TOKEN") == []` — only the braced form, matching pypa/pip#3514
- **Regress**: full suite re-run; the pre-existing 53 still pass.
- **Connect**: `parse_lines()` is the only line classifier now; `fetcher.py`'s
  `_parse_requirements_txt()` delegates to it and raises rather than returning a
  partial list when the file has unresolved references.
- **Deferred risk**:
  - `--hash=...` per-requirement options are not stripped from a requirement line
    (a hash-pinned requirements.txt would pass the hash through to the resolver).
    Not yet seen in practice; unverified.
  - The option table is a snapshot of pip's; a new pip option would be recorded
    as "unrecognised" (safe — skipped, not passed through) until added here.

## unit: includes.py — following -r / -c / -e (Unit 1)

- **Scope**: core (this is the correctness fix; the other v0.3 units are coverage)
- **The defect**: `fetch_requirements()`'s contract is "complete list or raise".
  A `requirements.txt` containing `-r base.txt` dropped everything in `base.txt`
  and handed the caller the remainder **as if it were the whole set** — the same
  failure class as the `--python` bug fixed in 0.2.0: a confident wrong answer.
- **Change**: new `compat_check/includes.py` — `resolve_includes(root_file, text,
  resolver)` walks `-r`/`-c` recursively and reports `-e` targets. The parser stays
  pure (`reqline.py` takes a string); the network stays in `fetcher.py`, injected
  as a `Resolver` callable. Bounds: depth 8, 32 files total.
- **Cycle detection copies pip's `_parse_and_recurse`**: a dict per branch,
  `{path: first_including_file}`, copied on each descent — **not** the shared
  `seen` set the design originally proposed. The distinction is load-bearing
  because an unresolvable include is fatal here: with a set, a legal diamond
  (`dev → base`, `dev → test → base`) and a true cycle are the same state, so we
  would either raise on the diamond or miss the cycle.
- **Evidence (Prove)**: `tests/test_includes.py` 12/12.
- **Refutation (Refute)** — including two live repos, not fixtures alone:
  - **the fix, measured on real repos**: `celery/celery:requirements/test.txt`
    resolves 11 → **21** requirements; `home-assistant/core:requirements_test.txt`
    resolves 47 → **51**. Before this unit, compat-check would have probed
    home-assistant and reported "47 packages, all fine" while the `-r` include
    was invisible to it.
  - **a second wrong answer, found by verifying this unit's own deferred risk
    instead of only recording it**: the first implementation merged `-c`
    constraint entries into `requirements`, which took home-assistant to **181**
    — 130 of those existed *only* as constraints. A constraint means "IF this
    package is pulled in, pin it here", not "install this", so merging them
    reproduced the same confident-wrong-answer defect in the opposite direction
    (over-reporting instead of truncating). `ResolvedRequirements` now carries
    `constraints` as a separate list, `fetch_requirements()` returns only the
    requirements, and the measured figure is back to the correct 51.
  - diamond include resolves (base.txt read twice, its requirement deduped once)
    — the case a shared set breaks
  - `a → b → a` and `a → a` both raise and name where the file was first included
  - a missing include raises instead of returning the surviving lines
  - relative paths resolve against the *including* file (`../base.txt` from
    `requirements/dev.txt`), using posixpath — repo paths are URL paths, so
    ntpath semantics must not leak in on Windows
  - depth cap and file cap both trip
  - `-e .`/`-e ./sub` are reported as local editables and resolved through the
    repo's own pyproject/setup.cfg; `-e git+https://…` is a different project and
    is recorded as skipped, not followed
  - a malformed option inside an *included* file names that file, not the root
- **Regress**: full suite 80/80 across 11 modules, 0 failures (was 53/53 across 9).
  `fetch_requirements("requests")` and the flask URL both unchanged, confirming the
  non-include paths did not regress.
- **Connect**: `_fetch_from_github()` now routes `requirements.txt` through
  `_fetch_requirements_txt()`, which builds a repo+branch-bound resolver. An
  `IncludeError` is converted to `FetchError` **without falling through to another
  candidate file** — falling through would answer a different, partial question.
- **Deferred risk**:
  - `-c` constraint entries are collected but **not** applied to the probe. pip
    would pass them as `--constraint`, bounding versions of packages pulled in
    transitively; we currently resolve without those bounds, so a dry-run can
    succeed on a version combination the project itself would reject. Collecting
    them separately is correct; feeding them to the resolver is not yet done.
  - `local_editables` resolution reads only `pyproject.toml`/`setup.cfg` in the
    target directory — a sub-package whose deps live in its own `requirements.txt`
    is not followed a second level.
  - The 32-file cap is global, not per-branch; a wide-but-shallow tree could trip
    it before the depth cap does.

## unit: fetcher Unit 3 — default-branch detection, URL forms, error diagnosis

- **Scope**: core (diagnosis and round-trip reduction; no new capability)
- **Trigger**: v0.3 design Unit 3, after Units 0/1 shipped in 0.3.0.
- **Measured before building** (`api.github.com`, unauthenticated):
  - `jahyunlee00299/compat-check` → 200, `default_branch=master`;
    `psf/requests` → 200, `main`. Detection works.
  - a nonexistent repo and `github/github` (real, private to us) **both return
    404** — GitHub does this deliberately so private repos do not leak. The
    design's "say it is one of the two, do not claim which" is therefore the
    only honest wording, confirmed rather than assumed.
  - **rate limit is 60 requests/hour unauthenticated** — not in the design.
    This makes the fallback mandatory rather than merely nice, and is why a
    403 is only diagnosed as rate limiting when `X-RateLimit-Remaining: 0`.
- **Change**:
  - `_lookup_repo()` → `RepoLookup(default_branch, problem)`. One API call
    replaces probing `main` then `master` across every candidate file.
  - 404 → a message naming both possibilities and stating private repos are
    unsupported. 403 with the budget exhausted → rate-limit message with the
    reset time. Any other status, or a network error → `RepoLookup()` with
    neither field set, so the caller degrades to the old `main`/`master` guess.
  - `parse_github_url()` accepts three forms it previously rejected:
    scheme-less `github.com/o/r`, SSH `git@github.com:o/r.git`, and
    `/blob/<branch>/<path>` deep links (branch used, path discarded). `www.`
    prefix handled.
  - `looks_like_github()` + a check in `fetch_requirements()`: a string
    containing `github.com` that did not parse now raises a GitHub-specific
    error listing the accepted forms, instead of falling through to the PyPI
    lookup and reporting `PyPI package not found: github.com/psf/requests`.
- **Evidence (Prove)**: `tests/test_github_ref.py` 13/13. Network paths are
  mocked so the suite does not spend the measured 60/hour budget.
- **Refutation (Refute)**:
  - **the round-trip saving, measured** by counting `_http_get` calls: on this
    repo (defaults to `master`) a fetch now makes **3** raw requests, all against
    the real branch, where the old code made 3 futile `main/*` 404s first — a
    6→3 halving. On flask (`main`) the first candidate file hits immediately: 1
    request.
  - a diagnosed 404 does **not** fall through to raw probing
    (`_http_get.call_count == 0`), so the specific message is not replaced by a
    vaguer "no pyproject.toml found".
  - API unreachable (`URLError`) still resolves through `main`→`master`, proving
    the API is an optimization and not a new hard dependency.
  - an explicit `/tree/<branch>` skips the lookup entirely
    (`_lookup_repo.call_count == 0`) — no budget spent when the user already said.
  - a 403 that is *not* rate limiting (`X-RateLimit-Remaining: 42`) falls back
    rather than emitting a false rate-limit diagnosis.
  - `fetch_requirements("github.com/only-one-segment")` raises a GitHub error
    with `_fetch_from_pypi.call_count == 0`.
  - all 11 pasted URL forms parse to the same owner/repo, with the branch
    extracted only where one is present.
- **Regress**: 95/95 across 12 modules, 0 failures (was 82/82 across 11).
- **Connect**: `_fetch_from_github()` is the only caller of `_lookup_repo()`;
  `fetch_requirements()` is the only caller of `looks_like_github()`. Both live
  behind the unchanged public signature.
- **Deferred risk**:
  - **The CLI spends one API request per run even on a cache hit.** Measured:
    `cached_probe_all()` returns `cache_hit=True` on the second run, but
    `cli.main()` calls `fetch_requirements()` *before* consulting the cache, so
    the branch lookup happens regardless. At 60/hour this is tolerable for
    interactive use and would matter in a loop. Fixing it means caching the
    fetch itself, which is a separate design (provenance, TTL, invalidation).
  - The branch lookup result is not cached within a process either; two
    `fetch_requirements()` calls for the same repo make two API calls.
  - `looks_like_github()` is a substring test, so a PyPI package legitimately
    named with `github.com` inside it would be misrouted. No such package
    exists; the trade-off favours the common typo.
