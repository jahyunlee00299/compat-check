"""Tests for compat_check.setuppy — reading install_requires without executing it.

Shapes here are not invented: 20 well-known repos still shipping a setup.py
were surveyed. Of the 5 passing install_requires to setup(), 3 are statically
resolvable (a Name bound to a list literal, twice; an inline list once) and 2
are not (a Call, a BinOp). Both kinds are covered below.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.setuppy import SetupPyError, parse_setup_py


def test_inline_literal_list():
    src = "from setuptools import setup\nsetup(install_requires=['requests>=2.0', 'flask'])"
    assert parse_setup_py(src) == ["requests>=2.0", "flask"]


def test_name_bound_to_module_level_list():
    """The most common real shape — records and boto3 both use it."""
    src = "from setuptools import setup\nREQS = ['a>=1', 'b']\nsetup(install_requires=REQS)"
    assert parse_setup_py(src) == ["a>=1", "b"]


def test_annotated_assignment_is_followed():
    src = "from setuptools import setup\nREQS: list = ['a']\nsetup(install_requires=REQS)"
    assert parse_setup_py(src) == ["a"]


def test_setuptools_dot_setup_is_found():
    src = "import setuptools\nsetuptools.setup(install_requires=['a'])"
    assert parse_setup_py(src) == ["a"]


def test_tuple_is_accepted():
    src = "from setuptools import setup\nsetup(install_requires=('a', 'b'))"
    assert parse_setup_py(src) == ["a", "b"]


def test_empty_list_is_an_answer_not_a_failure():
    """supervisor ships exactly this: install_requires=[]."""
    src = "from setuptools import setup\nsetup(install_requires=[])"
    assert parse_setup_py(src) == []


def test_blank_entries_are_dropped_and_whitespace_trimmed():
    src = "from setuptools import setup\nsetup(install_requires=['  a  ', '', 'b'])"
    assert parse_setup_py(src) == ["a", "b"]


def test_hostile_setup_py_is_never_executed():
    """The reason this module exists instead of importing the file.

    setuptools' read_attr() falls back to executing the module when the AST
    read fails. We read strangers' repos, so there is no such fallback.
    """
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "PWNED"
        src = (
            "import pathlib, os\n"
            f"pathlib.Path(r'{sentinel}').write_text('executed')\n"
            "os.environ['COMPAT_CHECK_LEAKED'] = 'yes'\n"
            "from setuptools import setup\n"
            "setup(install_requires=['requests'])\n"
        )
        assert parse_setup_py(src) == ["requests"]
        assert not sentinel.exists(), "setup.py was executed"
        assert "COMPAT_CHECK_LEAKED" not in os.environ


def _assert_raises(src: str, needle: str = ""):
    try:
        got = parse_setup_py(src)
    except SetupPyError as e:
        if needle:
            assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"should have raised, returned {got!r}")


def test_runtime_computed_values_raise_rather_than_guess():
    # Each of these is a shape seen in the wild (celery: Call, gevent: BinOp).
    _assert_raises("from setuptools import setup\nsetup(install_requires=read_reqs())", "runtime")
    _assert_raises("from setuptools import setup\nA=['a']\nsetup(install_requires=A+['b'])", "runtime")
    _assert_raises(
        "from setuptools import setup\nsetup(install_requires=[x for x in open('r')])", "runtime"
    )
    _assert_raises(
        "from setuptools import setup\nimport sys\n"
        "setup(install_requires=['a'] if sys.version_info > (3,) else ['b'])",
        "runtime",
    )


def test_kwargs_expansion_raises():
    _assert_raises(
        "from setuptools import setup\nkw = {'install_requires': ['a']}\nsetup(**kw)", "kwargs"
    )


def test_name_not_assigned_at_module_level_raises():
    _assert_raises("from setuptools import setup\nsetup(install_requires=REQS)", "REQS")


def test_name_assigned_only_inside_a_function_is_not_used():
    """A conditional or in-function rebinding is not reliably what setup() sees."""
    src = (
        "from setuptools import setup\n"
        "def f():\n    REQS = ['wrong']\n"
        "setup(install_requires=REQS)\n"
    )
    _assert_raises(src, "REQS")


def test_missing_pieces_raise_distinctly():
    _assert_raises("x = 1", "no setup() call")
    _assert_raises("from setuptools import setup\nsetup(name='x')", "no install_requires")
    _assert_raises("from setuptools import setup\nsetup(install_requires=[", "not valid Python")


def test_non_string_entry_raises():
    _assert_raises("from setuptools import setup\nsetup(install_requires=['a', 3])", "non-string")


def test_non_list_value_raises():
    _assert_raises("from setuptools import setup\nsetup(install_requires='requests')", "not a list")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} setup.py tests passed")
