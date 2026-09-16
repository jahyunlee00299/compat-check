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

## What it deliberately does not check (yet)

- BLAS/LAPACK backend compatibility — this is a post-install diagnostic
  (`numpy.show_config()`), not something knowable before installing
- GPU/CUDA driver compatibility beyond what the resolver itself reports —
  PyTorch-style packages that ship on a separate index aren't covered
- `setup.py`-only packages with no `pyproject.toml`/`requirements.txt`/
  `setup.cfg` (would require unsafe code execution to parse reliably)

## Install

Not yet published to PyPI — install directly from the repo:

```
uv tool install git+https://github.com/jahyunlee00299/compat-check
```

(or `pipx install git+https://github.com/jahyunlee00299/compat-check`, or clone and
`pip install .` into a venv)

## Usage

```
compat-check <github-url-or-pypi-package-name> [--python 3.11] [--no-cache] [--tree]
```

Exit codes: `0` clean, `1` conflicts found, `2` source could not be resolved
at all (bad URL, nonexistent package).

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
minute-to-minute. Use `--no-cache` to force a fresh probe.

## How it works

1. Fetch the requirement list — from `pyproject.toml`, `requirements.txt`,
   or `setup.cfg` on the GitHub repo, or from PyPI's JSON API for a bare
   package name.
2. Create a disposable virtual environment.
3. Run `pip install --dry-run` (or `uv pip install --dry-run`) against it —
   this resolves and would-download, but never actually installs anything
   or runs arbitrary setup code from the target package.
4. Report the result, retrying with failing packages dropped one at a time
   so every conflict in a multi-package source gets surfaced, not just the
   first one the resolver hits.

## License

MIT
