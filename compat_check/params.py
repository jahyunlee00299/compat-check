"""Single source of truth for run parameters that decide a probe's outcome.

Why this module exists
----------------------
`python_version` used to flow from the CLI straight into two places that
disagreed about what it meant:

  * `runner._PipBackend.create_venv()` ignores it entirely — the stdlib
    `venv` module can only clone the interpreter that is already running,
    it cannot fetch Python 3.9 the way `uv venv --python 3.9` can.
  * `cache.make_cache_key()` hashed it anyway.

So `--python 3.9` and `--python 3.13` produced byte-identical probes on a
pip-backend machine but landed in two different cache rows, each labelled
with a version it was never actually run against. A later hit would then
serve a 3.9-labelled answer that was really measured on 3.13. The value was
not merely ignored — it was ignored *and* recorded as if honoured.

`ResolvedParams` is the one place that decides what a parameter means for a
given backend, so the runner, the cache key and the user-facing warning all
read the same resolution instead of each re-deriving it.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# compat-check's own version participates in the cache key: a change to the
# resolution logic must not serve answers computed by the previous version.
from compat_check import __version__

_PYTHON_VERSION_RE = re.compile(r"^3\.(\d{1,2})(\.\d{1,2})?$")

MIN_MINOR = 8
MAX_MINOR = 20  # generous upper bound; rejects typos like "3.111", not the future


class ParamError(ValueError):
    """A parameter is malformed — raised before any subprocess is spawned."""


@dataclass(frozen=True)
class ResolvedParams:
    """What a set of CLI parameters actually means for the selected backend.

    `requested_python` is what the user typed; `effective_python` is what the
    probe will really run against. They differ exactly when the backend cannot
    honour the request, and `warnings` then explains why.
    """
    requested_python: str
    effective_python: str
    backend: str
    honors_python_version: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def python_was_ignored(self) -> bool:
        return self.requested_python != self.effective_python


def validate_python_version(value: str) -> str:
    """Reject a malformed --python before a venv is built for it.

    Without this, `--python 3.111` reached `uv venv --python 3.111`, which
    fails deep inside a subprocess with uv's own error text, and on the pip
    backend it failed nowhere at all — it was silently accepted and hashed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ParamError("--python must be a non-empty version string like '3.11'")

    value = value.strip()
    m = _PYTHON_VERSION_RE.match(value)
    if m is None:
        raise ParamError(
            f"--python {value!r} is not a valid Python version. "
            "Expected the form '3.11' (or '3.11.7'); compat-check targets Python 3.x only."
        )

    minor = int(m.group(1))
    if not (MIN_MINOR <= minor <= MAX_MINOR):
        raise ParamError(
            f"--python {value!r} is out of the supported range "
            f"(3.{MIN_MINOR} to 3.{MAX_MINOR})."
        )
    return value


def running_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def resolve_params(python_version: str, backend_name: str) -> ResolvedParams:
    """Decide what `python_version` means for `backend_name`, once.

    The uv backend honours the request. The pip backend cannot, so the
    effective version collapses to the running interpreter — and the cache
    key built from these params collapses with it, which is the whole point:
    two requests that run the identical probe must share one cache row.
    """
    requested = validate_python_version(python_version)
    warnings: list[str] = []

    if backend_name == "uv":
        return ResolvedParams(
            requested_python=requested,
            effective_python=requested,
            backend=backend_name,
            honors_python_version=True,
        )

    running = running_python_version()
    if requested != running:
        warnings.append(
            f"--python {requested} was requested, but the pip fallback backend can only "
            f"probe the interpreter it is running on (Python {running}). "
            f"The result below describes Python {running}, not {requested}. "
            f"Install `uv` to target other Python versions."
        )
    return ResolvedParams(
        requested_python=requested,
        effective_python=running,
        backend=backend_name,
        honors_python_version=False,
        warnings=warnings,
    )


def cache_identity(requirements: list[str], params: ResolvedParams,
                    constraints: list[str] | None = None) -> dict:
    """The exact set of facts that determine a probe's outcome.

    Deliberately keyed on `effective_python`, never `requested_python`: on the
    pip backend those two differ and only the effective one was measured.
    Includes `compat_check_version` so a resolution-logic change invalidates
    rather than silently reuses older rows.
    """
    return {
        "requirements": sorted(requirements),
        # Constraints change which versions resolve (measured: `requests` with
        # `urllib3<1.0` resolves to requests==2.15.1, not the latest), so two
        # runs differing only by constraints are NOT the same probe.
        "constraints": sorted(constraints or []),
        "python_version": params.effective_python,
        "backend": params.backend,
        "compat_check_version": __version__,
    }
