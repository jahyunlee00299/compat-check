"""Tests for compat_check.params — the parameter SSOT.

The bug this module exists to prevent: on a pip-backend machine, `--python 3.9`
and `--python 3.13` ran the identical probe (stdlib venv can only clone the
running interpreter) yet were stored under two different cache keys, each
labelled with a version it was never measured against.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.params import (
    ParamError,
    cache_identity,
    resolve_params,
    running_python_version,
    validate_python_version,
)


def test_valid_versions_accepted():
    for v in ["3.8", "3.11", "3.13", "3.11.7", "3.20"]:
        assert validate_python_version(v) == v


def test_malformed_versions_rejected():
    # "3.111" is the typo that previously reached uv as a real argument.
    for bad in ["3.111", "2.7", "abc", "", "  ", "3", "3.x", "4.0", "3.7"]:
        try:
            validate_python_version(bad)
        except ParamError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_uv_backend_honors_requested_version():
    p = resolve_params("3.9", "uv")
    assert p.effective_python == "3.9"
    assert p.honors_python_version is True
    assert p.python_was_ignored is False
    assert p.warnings == []


def test_pip_backend_collapses_to_running_interpreter():
    p = resolve_params("3.9", "pip")
    assert p.effective_python == running_python_version()
    assert p.honors_python_version is False
    # The mismatch must be surfaced, not swallowed.
    if p.effective_python != "3.9":
        assert p.python_was_ignored is True
        assert p.warnings, "an ignored --python must produce a warning"
        assert "3.9" in p.warnings[0]


def test_pip_backend_no_warning_when_request_matches_reality():
    p = resolve_params(running_python_version(), "pip")
    assert p.python_was_ignored is False
    assert p.warnings == []


def test_cache_identity_collapses_ignored_versions_on_pip():
    """THE regression guard: two requests that run the same probe share one identity."""
    a = cache_identity(["requests"], resolve_params("3.9", "pip"))
    b = cache_identity(["requests"], resolve_params("3.13", "pip"))
    assert a == b


def test_cache_identity_still_separates_versions_on_uv():
    """Control: uv really does honour the version, so these must NOT collapse."""
    a = cache_identity(["requests"], resolve_params("3.9", "uv"))
    b = cache_identity(["requests"], resolve_params("3.13", "uv"))
    assert a != b


def test_cache_identity_includes_tool_version():
    from compat_check import __version__
    ident = cache_identity(["requests"], resolve_params("3.11", "uv"))
    assert ident["compat_check_version"] == __version__


def test_cache_identity_ignores_requirement_order():
    p = resolve_params("3.11", "uv")
    assert cache_identity(["b==1", "a==2"], p) == cache_identity(["a==2", "b==1"], p)


def test_resolve_rejects_invalid_before_backend_dispatch():
    for backend in ("uv", "pip"):
        try:
            resolve_params("3.999", backend)
        except ParamError:
            continue
        raise AssertionError("invalid version must raise regardless of backend")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} params tests passed")
