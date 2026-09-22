"""End-to-end tests for the compat-check CLI — exercises the full
fetcher -> cache/runner -> report pipeline, including exit codes."""
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.cli import main
from compat_check.fetcher import FetchResult


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_success_exits_zero():
    rc, out, _ = _run(["requests", "--no-cache"])
    assert rc == 0
    assert "OK" in out
    assert "requests" in out


def test_fetch_failure_exits_two():
    rc, _, err = _run(["this-package-definitely-does-not-exist-xyz123"])
    assert rc == 2
    assert "could not resolve" in err


def test_conflicting_requirements_exit_one_and_report_package():
    with mock.patch("compat_check.cli.fetch_requirements_detailed",
                     return_value=FetchResult(requirements=["numpy>=2.0", "numpy<1.20"])):
        rc, out, _ = _run(["fake-conflicting-package", "--no-cache"])
    assert rc == 1
    assert "PROBLEMS FOUND" in out
    assert "[numpy]" in out


def test_cache_hit_reported_on_second_call():
    _run(["requests"])  # warm the cache
    _, out, _ = _run(["requests"])
    assert "(cached)" in out


def test_tree_flag_shows_source_as_root_with_children():
    rc, out, _ = _run(["https://github.com/pallets/flask", "--no-cache", "--tree"])
    assert rc == 0
    assert "https://github.com/pallets/flask" in out
    assert "markupsafe" in out


def test_tree_flag_on_conflict_reports_unavailable_without_crashing():
    with mock.patch("compat_check.cli.fetch_requirements_detailed",
                     return_value=FetchResult(requirements=["numpy>=2.0", "numpy<1.20"])):
        rc, out, err = _run(["fake-conflict", "--no-cache", "--tree"])
    assert rc == 1  # the probe_all() failure still drives the exit code
    assert "PROBLEMS FOUND" in out
    assert "tree unavailable" in err


def test_invalid_python_version_exits_3_without_network():
    """A malformed --python must fail before any network call is made."""
    with mock.patch("compat_check.cli.fetch_requirements_detailed") as fetch:
        rc, out, err = _run(["requests", "--python", "3.111"])
    assert rc == 3, (rc, out, err)
    assert fetch.call_count == 0, "network was hit despite an invalid parameter"
    assert "not a valid Python version" in err


def test_invalid_python_exit_code_is_distinct_from_fetch_and_conflict():
    rc_param, _, _ = _run(["requests", "--python", "2.7"])
    assert rc_param == 3
    # 2 = unresolvable source, 1 = conflict, 3 = bad parameter — all distinct.
    rc_fetch, _, _ = _run(["this-package-definitely-does-not-exist-xyz123"])
    assert rc_fetch == 2
    assert rc_param != rc_fetch


def test_report_states_the_python_version_actually_probed():
    with mock.patch("compat_check.cli.fetch_requirements_detailed",
                     return_value=FetchResult(requirements=["certifi"])):
        rc, out, err = _run(["fake-src", "--no-cache"])
    assert rc == 0
    assert "python:" in out, out


def test_constraints_reach_the_prober_and_are_reported():
    """A -c file bounds the resolution; the report must say so."""
    fetched = FetchResult(requirements=["requests"], constraints=["urllib3<2.0"],
                           source_file="requirements.txt")
    seen = {}

    def fake_cached(reqs, **kw):
        seen.update(kw)
        return {"ok": True, "backend": "uv", "failures": [], "resolved": [],
                "cache_hit": False, "params": None}

    with mock.patch("compat_check.cli.fetch_requirements_detailed", return_value=fetched),          mock.patch("compat_check.cli.cached_probe_all", side_effect=fake_cached):
        rc, out, err = _run(["fake-src"])
    assert rc == 0
    assert seen.get("constraints") == ["urllib3<2.0"], seen
    assert "constraints applied: 1" in out, out
    assert "source file: requirements.txt" in out


def test_constraints_reach_the_prober_on_the_no_cache_path_too():
    """--no-cache bypasses the cache, not the constraints."""
    fetched = FetchResult(requirements=["requests"], constraints=["urllib3<2.0"])
    seen = {}

    def fake_probe(reqs, **kw):
        seen.update(kw)
        return {"ok": True, "backend": "uv", "failures": [], "resolved": [],
                "params": None}

    with mock.patch("compat_check.cli.fetch_requirements_detailed", return_value=fetched),          mock.patch("compat_check.cli.probe_all", side_effect=fake_probe):
        rc, out, err = _run(["fake-src", "--no-cache"])
    assert rc == 0
    assert seen.get("constraints") == ["urllib3<2.0"], seen


def test_skipped_notes_are_surfaced_to_stderr():
    fetched = FetchResult(requirements=["requests"],
                           skipped=["requirements.txt: -e git+https://x  (not followed)"])
    with mock.patch("compat_check.cli.fetch_requirements_detailed", return_value=fetched),          mock.patch("compat_check.cli.cached_probe_all",
                     return_value={"ok": True, "backend": "uv", "failures": [],
                                   "resolved": [], "cache_hit": False, "params": None}):
        rc, out, err = _run(["fake-src"])
    assert "not followed" in err, err


def test_no_constraints_means_no_constraint_line():
    fetched = FetchResult(requirements=["requests"])
    with mock.patch("compat_check.cli.fetch_requirements_detailed", return_value=fetched),          mock.patch("compat_check.cli.cached_probe_all",
                     return_value={"ok": True, "backend": "uv", "failures": [],
                                   "resolved": [], "cache_hit": False, "params": None}):
        rc, out, err = _run(["fake-src"])
    assert "constraints applied" not in out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(f"running {name}...")
            fn()
            print(f"  PASS")
    print("\nall CLI tests passed")
