"""Force the pip fallback backend (simulates a machine without uv) and re-run
the same scenarios as test_runner.py — this is the actual robustness proof
the user asked for, not just "it works when uv happens to be installed"."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.runner import probe_once, _PipBackend

_PIP = _PipBackend()


def test_pip_all_ok():
    r = probe_once(["requests>=2.0"], backend=_PIP)
    assert r.ok is True
    # Both backends emit "name==version"; pip's native "name-version" form is
    # normalized in runner._normalize_resolved so callers see one contract.
    assert any(p.startswith("requests==") for p in r.resolved_packages), r.resolved_packages


def test_pip_resolved_format_matches_uv_contract():
    """The two backends must not disagree about how a resolved package looks."""
    r = probe_once(["requests>=2.0"], backend=_PIP)
    assert r.resolved_packages
    for pkg in r.resolved_packages:
        assert "==" in pkg, f"pip backend leaked a non-normalized token: {pkg}"
        assert not pkg.endswith("=="), pkg
    # Hyphenated names must survive normalization intact.
    names = [p.split("==")[0] for p in r.resolved_packages]
    assert "charset-normalizer" in names or "charset_normalizer" in names, names


def test_pip_empty_requirements():
    r = probe_once([], backend=_PIP)
    assert r.ok is True


def test_pip_single_unsatisfiable_version():
    r = probe_once(["numpy==0.0.1"], backend=_PIP)
    assert r.ok is False
    assert r.failing_package == "numpy", r.stdout


def test_pip_mutually_conflicting_constraints():
    r = probe_once(["numpy>=2.0", "numpy<1.20"], backend=_PIP)
    assert r.ok is False
    assert r.failing_package == "numpy", r.stdout


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(f"running {name}...")
            fn()
            print(f"  PASS")
    print("\nall pip-backend tests passed")
