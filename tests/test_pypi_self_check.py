"""PyPI mode probes the package itself, not only its requires_dist.

Measured 2026-10-11 against Python 3.15.0rc3: `compat-check torch --python 3.15`
printed OK (torch's dependencies resolve) while
`uv pip install --dry-run torch` failed — all torch wheels stop at cp314. The
JSON payloads below are trimmed copies of PyPI's real shape; the network is
mocked so these run offline.
"""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check import fetcher
from compat_check.fetcher import FetchError, fetch_requirements, fetch_requirements_detailed


def _payload(name, requires_dist):
    return json.dumps({"info": {"name": name, "requires_dist": requires_dist}})


def _fetch(source, payload):
    seen = []

    def fake_get(url):
        seen.append(url)
        return payload

    with mock.patch.object(fetcher, "_http_get", side_effect=fake_get):
        return fetch_requirements_detailed(source), seen


def test_the_package_itself_is_probed_first():
    result, _ = _fetch("torch", _payload("torch", ["filelock", "sympy>=1.13"]))
    assert result.requirements == ["torch", "filelock", "sympy>=1.13"]


def test_empty_requires_dist_is_checkable_not_blind():
    """gemmi, xlsxwriter and propka were exit 2 'declares no base requires_dist'."""
    for body in (None, []):
        result, _ = _fetch("gemmi", _payload("gemmi", body))
        assert result.requirements == ["gemmi"]


def test_extras_only_requirements_are_still_dropped():
    result, _ = _fetch("pkg", _payload("pkg", [
        "numpy", 'pytest; extra == "test"', "docs-dep ; extra=='docs'"]))
    assert result.requirements == ["pkg", "numpy"]


def test_canonical_name_from_pypi_is_used():
    result, _ = _fetch("pyyaml", _payload("PyYAML", []))
    assert result.requirements == ["PyYAML"]


def test_name_equals_version_pins_the_probe_and_reads_that_release():
    result, urls = _fetch("torch==2.4.0", _payload("torch", ["filelock"]))
    assert result.requirements == ["torch==2.4.0", "filelock"]
    assert urls == ["https://pypi.org/pypi/torch/2.4.0/json"]


def test_unknown_package_still_raises_fetch_error():
    """Not found on PyPI stays exit 2 (BLIND), unlike an empty requires_dist."""
    with mock.patch.object(fetcher, "_http_get", return_value=None):
        try:
            fetch_requirements("definitely-not-a-real-pkg-xyz")
        except FetchError as e:
            assert "not found" in str(e)
            return
    raise AssertionError("expected FetchError")


def test_malformed_name_is_rejected_before_any_request():
    with mock.patch.object(fetcher, "_http_get") as get:
        try:
            fetch_requirements("../etc/passwd")
        except FetchError as e:
            assert "not a valid PyPI package name" in str(e)
            assert get.call_count == 0
            return
    raise AssertionError("expected FetchError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} pypi self-check tests passed")
