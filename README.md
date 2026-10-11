# compat-check

[![test](https://github.com/jahyunlee00299/compat-check/actions/workflows/test.yml/badge.svg)](https://github.com/jahyunlee00299/compat-check/actions/workflows/test.yml)

Check whether a GitHub repo or PyPI package would install cleanly **in this
environment** — before you actually install it.

```
$ compat-check https://github.com/pallets/flask
compat-check: https://github.com/pallets/flask
backend: uv
requirements checked: blinker>=1.9.0, click>=8.1.3, itsdangerous>=2.2.0, jinja2>=3.1.2, markupsafe>=2.1.1, werkzeug>=3.1.0

OK — 6 package(s) would install cleanly:
  + blinker==1.9.0
  + click==8.5.0
  ...
```

```
$ compat-check some-package-with-a-real-conflict
PROBLEMS FOUND — 1 package(s) cannot be resolved:

  [numpy]
      × No solution found when resolving dependencies:
      ╰─▶ Because you require numpy>=2.0 and numpy<1.20, we can conclude that your
          requirements are unsatisfiable.
```

A resolve that succeeds but would compile something from source is not a plain
OK. Measured against Python 3.15.0rc3 on 2026-10-11:

```
$ compat-check pyyaml --python 3.15
RESOLVES WITH WARNINGS — 1 package(s) resolve, but 1 need a source build:
  + pyyaml==6.0.3   [source build]

WARN — 1 package(s) have no wheel for Python 3.15 on linux x86_64 (glibc 2.39); install will build from source:
  ! pyyaml==6.0.3   newest wheels: cp314-cp314, cp314-cp314t, cp313-cp313
```

## Why this instead of `uv`/`pip` themselves

`uv` and `pip` already resolve dependencies — that's not the gap. Two things
are:

- **You still have to actually run the install (or a dry-run) yourself**,
  reading whatever error comes back. `compat-check` does that in a throwaway
  venv and hands you a plain-language pass/fail, without touching your real
  environment.
- **Both resolvers are fail-fast**: a single dry-run call reports only the
  *first* unsatisfiable requirement. If two unrelated packages in the same
  `requirements.txt` are both broken, one hides behind the other.
  `compat-check` drops each failure and retries until every one surfaces.

It does **not** try to out-resolve `uv`/`pip` — it wraps them (preferring
`uv` when available, falling back to the standard-library `venv` + `pip`
when it isn't) and reports what actually happened, not a static prediction
from metadata.

## What it checks

- Whether every requirement resolves at all (missing versions, yanked
  releases, platform/ABI mismatches — reported with the resolver's own
  explanation)
- Whether requirements in the same source conflict with each other
- For a PyPI name: the package **itself**, not only its `requires_dist`.
  `compat-check torch --python 3.15` fails because torch ships no `cp315`
  wheel; checking only torch's dependencies used to print OK. A package with no
  dependencies at all (gemmi, xlsxwriter) is checkable for the same reason.
  `name==version` pins the release.
- Whether each resolved package has a **wheel** for the target Python on this
  platform, or would be built from source (see below)
- Which package really blocks a failure. uv blames the package it cannot
  satisfy, which is often a dependency of a dependency; the report names the
  requirement you asked for and the root cause:

  ```
  [markitdown] (blocked by onnxruntime: no cp315 wheel)
    wheels for onnxruntime v1.31.0 exist only for: cp311, cp312, cp313, cp313t, cp314, cp314t
  ```

### Wheel check

After a successful resolve, each resolved `name==version` is looked up on PyPI
(`/pypi/<name>/<version>/json`, a few requests in parallel) and matched against
the target Python and this machine's platform:

- compatible: `py3-none-any`, `cpXY-cpXY-<platform>` for the target, and
  `cpXY-abi3-<platform>` with `XY` at most the target; `manylinux`/`musllinux`
  of the host architecture (and glibc version when readable), `win32`/
  `win_amd64`/`win_arm64`, `macosx` of the host architecture and version
- never compatible: free-threaded builds (`cp315t`), yanked files, wheels for
  another platform
- packages without a compatible wheel are listed with the newest wheel tags
  that do exist (or "sdist only")
- git/file/URL entries cannot be asked and are listed as non-PyPI sources
- if PyPI cannot be reached for a package, that package is **"wheel status
  UNKNOWN"**, never assumed fine

The lookup is not cached (new wheels appear after a resolve) and only runs after
a clean resolve. `--no-wheel-check` skips it.

## What it deliberately does not check (yet)

- BLAS/LAPACK backend compatibility — this is a post-install diagnostic
  (`numpy.show_config()`), not something knowable before installing
- GPU/CUDA driver compatibility beyond what the resolver itself reports —
  PyTorch-style packages that ship on a separate index aren't covered
- `setup.py`-only packages with no `pyproject.toml`/`requirements.txt`/
  `setup.cfg` (would require unsafe code execution to parse reliably)

It also does not look *inside* the code once it installs. A package that
resolves cleanly can still carry a measured constant nobody sourced, or an
index pointing at a file that is gone. [fiducial][fid] asks that question, with
the same three exit codes and after the same failure: a check that reports
success because it checked nothing. Here that shape is a fail-fast resolver
hiding the second broken requirement behind the first; there it was a scanner
whose path globs had gone stale printing `OK — no violations across 0 file(s)`.

The two are separate packages, neither depending on the other, and they hold at
different moments — this one once when you adopt a dependency, that one on
every commit. [fiducial's FOUR_AXES.md][axes] lays out the whole arrangement.

[fid]: https://github.com/jahyunlee00299/fiducial
[axes]: https://github.com/jahyunlee00299/fiducial/blob/main/docs/FOUR_AXES.md

## Install

Not yet published to PyPI — install directly from the repo:

```
uv tool install git+https://github.com/jahyunlee00299/compat-check
```

(or `pipx install git+https://github.com/jahyunlee00299/compat-check`, or clone and
`pip install .` into a venv)

## Usage

```
compat-check <github-url-or-pypi-package-name[==version]> [--python 3.11] [--no-cache]
             [--tree] [--strict-wheels] [--no-wheel-check]
```

A PyPI source is a bare name (`requests`, the latest release) or an exact pin
(`thermosteam==0.53.5`, that release's own requirements plus the pin itself, so
a release with no wheel for this Python shows up too). Other operators (`>=`,
`~=`, extras) are rejected with a message rather than looked up as a name.

The GitHub source accepts the forms people actually paste:

```
https://github.com/owner/repo
https://github.com/owner/repo.git
https://github.com/owner/repo/tree/some-branch
https://github.com/owner/repo/blob/some-branch/setup.py
github.com/owner/repo
git@github.com:owner/repo.git
```

Without an explicit branch, the repository's real default branch is looked up
rather than guessed, so a repo defaulting to something other than `main` costs
no wasted requests. If the GitHub API is unreachable or its unauthenticated
budget (60 requests/hour) is exhausted, the lookup degrades to trying `main`
then `master` — it is an optimization, not a requirement.

Exit codes: `0` clean (including "resolves, but see the WARN section"), `1`
conflicts found, `2` source could not be resolved at all (bad URL, nonexistent
package), `3` invalid parameter (checked before any network call, so a typo
costs nothing).

"Resolves but needs a source build" deliberately has **no exit code of its
own**. `0`/`1`/`2` is a contract shared with the other verification gates
(`2` means BLIND, i.e. "could not check", and must never be reused), and any
caller written to that contract would misread a new code as a failure or as a
blind spot. The condition is therefore exit `0` with a `RESOLVES WITH WARNINGS`
headline and a `WARN` section, and `--strict-wheels` turns it into exit `1` —
for a source build and also for a package whose wheel status could not be
determined, because neither can be called clean.

A GitHub source reads the four declaration files at the repository **root**
only (`pyproject.toml`, `requirements.txt`, `setup.cfg`, `setup.py`). A root with
none of them is exit `2` with a message saying exactly that: requirements files
in subdirectories (for example `skills/*/requirements.txt`) were not looked at,
so it is a partial blind spot and not a finding that there are no dependencies.

A repository that cannot be read is reported as what it is: a missing *or*
private repo (GitHub returns the same 404 for both, so no tool can tell them
apart), an exhausted API budget with its reset time, or a GitHub reference
that could not be parsed — never as a missing PyPI package.

### `--python` and the pip fallback

`--python` is honoured only by the `uv` backend. The pip fallback builds its
venv with the standard-library `venv` module, which can only clone the
interpreter compat-check is itself running on — it cannot fetch another
version. Rather than accept the flag and quietly ignore it, the report states
the version actually probed:

```
$ compat-check requests --python 3.9     # on a machine without uv
compat-check: requests
backend: pip
python: 3.13 (requested 3.9 — NOT honoured)
```

with the reason on stderr. The cache is keyed on the version that was really
used, so two requests that run the identical probe share one cache entry
instead of being stored under two versions, only one of which was measured.
Install `uv` to target other Python versions for real.

`--tree` shows the full dependency tree (requires `uv` — no pip-backend
equivalent exists):

```
$ compat-check https://github.com/pallets/flask --tree
...
https://github.com/pallets/flask
├── blinker v1.9.0
├── click v8.5.0
├── itsdangerous v2.2.0
├── jinja2 v3.1.6
│   └── markupsafe v3.0.3
├── markupsafe v3.0.3
└── werkzeug v3.1.8
    └── markupsafe v3.0.3
```

Results are cached locally (`~/.cache/compat_check/`, 7-day TTL) since a
dry-run against the same environment and requirements won't change
minute-to-minute. Use `--no-cache` to force a fresh probe. The cache holds the
500 most recent entries (oldest evicted first) and is invalidated automatically
when compat-check's own version changes, so a resolver change never serves an
answer computed by an older build.

## How it works

1. Fetch the requirement list — from `pyproject.toml`, `requirements.txt`,
   `setup.cfg` or `setup.py` on the GitHub repo, or from PyPI's JSON API for
   a bare package name (the package itself plus its `requires_dist`).

   A `setup.py` is read by parsing it, never by running it: `install_requires`
   is resolved from the syntax tree, including the common
   `REQUIRES = [...]; setup(install_requires=REQUIRES)` form. When the value is
   computed at runtime (a function call, a concatenation, a comprehension) the
   answer is genuinely unknowable without executing a stranger's code, so
   compat-check says so instead of guessing. A `requirements.txt` that uses `-r other.txt` has those
   files fetched and spliced in, so the list is the complete one: on
   `home-assistant/core` that is 51 requirements rather than the 47 visible
   in the root file. An include that cannot be read is an error, never a
   silently shorter list.

   `-c` constraint files are read but kept separate — a constraint pins a
   package *if* something pulls it in, so treating those entries as
   requirements would inflate the same repo to 181 packages it never asked
   to install. They are then passed to the resolver as `--constraint`, the
   way pip and uv mean them: resolving `requests` under `urllib3<1.0` really
   does yield `requests==2.15.1` rather than the latest, so ignoring the file
   would check a different version set than the project installs.

   An editable install with extras (`-e .[pg]`) resolves those extras too —
   records' `pg` group genuinely requires `psycopg2-binary`. Where an extra
   cannot be read statically, the gap is reported rather than left silent.
2. Create a disposable virtual environment.
3. Run `pip install --dry-run` (or `uv pip install --dry-run`) against it —
   this resolves and would-download, but never actually installs anything
   or runs arbitrary setup code from the target package.
4. Report the result, retrying with failing packages dropped one at a time
   so every conflict in a multi-package source gets surfaced, not just the
   first one the resolver hits. The package dropped is always a requirement
   you actually asked for ("you require X"), never the transitive package uv
   happens to name; that one is reported as the root cause.
5. If it resolved, check each resolved package for a compatible wheel (see
   "Wheel check").

## Known limits

- **Public GitHub repositories and PyPI names only.** A private repository is
  indistinguishable from a missing one (GitHub returns the same 404) and local
  paths are not accepted. Not supported in this version.
- **The target platform is this machine.** `--python` selects the interpreter
  version, not the operating system or CPU: there is no Windows/macOS
  cross-targeting from Linux. The wheel check matches the host platform only.
- **GitHub mode does not search subdirectories** (see Usage); a monorepo whose
  dependencies live below the root is reported as exit `2`, not partially checked.
- **Only PyPI is consulted for wheels.** Packages resolved from another index,
  git or a file are listed as "not wheel-checked".
- The wheel matcher is a minimal in-repo implementation of the tag rules
  (no `packaging` dependency); when the host glibc or macOS version cannot be
  read it accepts any `manylinux`/`macosx` wheel of the right architecture.

## License

MIT
