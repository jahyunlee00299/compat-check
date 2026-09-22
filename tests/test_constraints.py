"""Tests for constraint application and extras resolution.

Constraints were previously collected and discarded. Measured with uv:
resolving `requests` against `urllib3<1.0` yields requests==2.15.1 instead of
the latest, so dropping the constraint file checked a different version set
than the project itself installs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.cache import make_cache_key
from compat_check.includes import _split_extras, resolve_includes
from compat_check.params import cache_identity, resolve_params
from compat_check.runner import probe_all
from compat_check.setuppy import SetupPyError, parse_extras_require

_SETUP_PY = (
    "from setuptools import setup\n"
    "REQS = ['base-pkg']\n"
    "EXTRAS = {'pg': ['psycopg2-binary'], 'redshift': ['sqlalchemy-redshift', 'psycopg2']}\n"
    "setup(install_requires=REQS, extras_require=EXTRAS)\n"
)


def test_split_extras_separates_path_from_groups():
    assert _split_extras(".") == (".", [])
    assert _split_extras(".[pg]") == (".", ["pg"])
    assert _split_extras("./sub[a,b]") == ("./sub", ["a", "b"])
    assert _split_extras("./sub[ a , b ]") == ("./sub", ["a", "b"])


def test_editable_extras_survive_to_the_caller():
    files = {"requirements.txt": "-e .[pg,redshift]\npytest\n"}
    got = resolve_includes("requirements.txt", files["requirements.txt"],
                            lambda p: files.get(p))
    assert got.local_editables == [(".", ["pg", "redshift"])]


def test_extras_require_is_read_from_setup_py():
    assert parse_extras_require(_SETUP_PY, ["pg"]) == ["psycopg2-binary"]
    assert parse_extras_require(_SETUP_PY, ["redshift"]) == [
        "sqlalchemy-redshift", "psycopg2",
    ]
    assert parse_extras_require(_SETUP_PY, ["pg", "redshift"]) == [
        "psycopg2-binary", "sqlalchemy-redshift", "psycopg2",
    ]


def test_no_extras_requested_reads_nothing():
    assert parse_extras_require(_SETUP_PY, []) == []


def test_undeclared_extra_raises_and_lists_what_exists():
    try:
        parse_extras_require(_SETUP_PY, ["nope"])
    except SetupPyError as e:
        assert "nope" in str(e)
        assert "pg" in str(e) and "redshift" in str(e)
        return
    raise AssertionError("an undeclared extra must raise")


def test_runtime_computed_extras_raise_rather_than_guess():
    src = "from setuptools import setup\nsetup(extras_require=build_extras())"
    try:
        parse_extras_require(src, ["pg"])
    except SetupPyError as e:
        assert "runtime" in str(e)
        return
    raise AssertionError("computed extras_require must raise")


def test_missing_extras_require_raises():
    src = "from setuptools import setup\nsetup(install_requires=['a'])"
    try:
        parse_extras_require(src, ["pg"])
    except SetupPyError as e:
        assert "no extras_require" in str(e)
        return
    raise AssertionError("should raise")


def test_constraints_change_the_cache_key():
    """Two runs differing only by constraints are not the same probe."""
    params = resolve_params("3.11", "uv")
    bare = make_cache_key(["requests"], params)
    bound = make_cache_key(["requests"], params, ["urllib3<1.0"])
    assert bare != bound


def test_cache_key_is_constraint_order_independent():
    params = resolve_params("3.11", "uv")
    a = make_cache_key(["r"], params, ["a<1", "b<2"])
    b = make_cache_key(["r"], params, ["b<2", "a<1"])
    assert a == b


def test_cache_identity_records_constraints_explicitly():
    ident = cache_identity(["r"], resolve_params("3.11", "uv"), ["x<1"])
    assert ident["constraints"] == ["x<1"]
    assert cache_identity(["r"], resolve_params("3.11", "uv"))["constraints"] == []


def test_constraints_actually_bound_the_resolution():
    """The behaviour the cache key exists to distinguish — a real probe."""
    bare = probe_all(["requests"])
    bound = probe_all(["requests"], constraints=["urllib3<1.0"])
    assert bare["ok"] and bound["ok"], (bare, bound)

    def requests_version(result):
        for pkg in result["resolved"]:
            if pkg.startswith("requests=="):
                return pkg
        return None

    assert requests_version(bare) != requests_version(bound), (
        f"constraint had no effect: {requests_version(bare)}"
    )


def test_empty_constraints_behave_as_none():
    a = probe_all(["certifi"], constraints=[])
    b = probe_all(["certifi"])
    assert a["ok"] == b["ok"]
    assert a["resolved"] == b["resolved"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} constraint/extras tests passed")
