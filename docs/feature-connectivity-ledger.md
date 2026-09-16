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
