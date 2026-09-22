"""Tests for failure attribution in the runner.

Two defects, both found by auditing features the ledger had not retested:

1. uv's "was not found in the package registry" wording matched none of the
   three regexes, so `failing_package` came back None and `probe_all()` broke
   out after one round, reporting a single `<unknown>` failure. A typo in one
   package name hid every other failure in the file.

2. Packages were dropped from the retry list with `startswith()`, so removing
   `pkg1` also removed `pkg10`..`pkg19`. Measured with 25 distinct
   unsatisfiable packages: the loop finished in 10 rounds having silently
   discarded 15, neither probed nor reported.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.runner import (
    _UvBackend,
    _drops,
    _requirement_name,
    probe_all,
)

_UV = _UvBackend()

# Captured verbatim from real `uv pip install --dry-run` runs.
_NOT_FOUND = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because definitely-not-a-real-pkg-xyz0 was not found in the package
      registry and you require definitely-not-a-real-pkg-xyz0, we can conclude
      that your requirements are unsatisfiable.
"""

_NO_VERSION = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because there is no version of numpy==0.0.1 and you require
      numpy==0.0.1, we can conclude that your requirements are unsatisfiable.
"""

_CONFLICT = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because you require numpy>=2.0 and numpy<1.20, we can conclude that your
      requirements are unsatisfiable.
"""


def test_not_found_wording_is_attributed_to_the_package():
    assert _UV._extract_failing_package(_NOT_FOUND) == "definitely-not-a-real-pkg-xyz0"


def test_no_version_wording_still_works():
    assert _UV._extract_failing_package(_NO_VERSION) == "numpy"


def test_conflict_wording_still_works():
    assert _UV._extract_failing_package(_CONFLICT) == "numpy"


def test_line_wrapped_message_is_still_matched():
    """uv wraps at ~80 columns, splitting the phrase from the name."""
    wrapped = (
        "  ╰─▶ Because there is no\n      version of some-package==1.0 and you require it"
    )
    assert _UV._extract_failing_package(wrapped) == "some-package"


def test_unmatched_output_returns_none_rather_than_guessing():
    assert _UV._extract_failing_package("something entirely unexpected") is None


def test_requirement_name_extraction():
    assert _requirement_name("numpy==0.0.1") == "numpy"
    assert _requirement_name("numpy>=2.0") == "numpy"
    assert _requirement_name("pkg[extra]==1") == "pkg"
    assert _requirement_name("  spaced >=1 ") == "spaced"
    # PEP 503: underscores and dashes are the same name.
    assert _requirement_name("charset_normalizer<4") == "charset-normalizer"


def test_drops_matches_whole_names_not_prefixes():
    """The bug: dropping pkg1 also dropped pkg10..pkg19."""
    assert _drops("xyz1", "xyz1") is True
    assert _drops("xyz1==1.0", "xyz1") is True
    assert _drops("xyz10", "xyz1") is False
    assert _drops("xyz19>=2", "xyz1") is False
    assert _drops("charset_normalizer<4", "charset-normalizer") is True


def test_every_nonexistent_package_is_reported_not_just_the_first():
    pkgs = [f"definitely-not-a-real-pkg-xyz{i}" for i in range(5)]
    result = probe_all(pkgs)
    assert result["ok"] is False
    named = [f["package"] for f in result["failures"]]
    assert len(named) == 5, named
    assert "<unknown>" not in named
    assert sorted(named) == sorted(pkgs)


def test_prefix_named_packages_are_each_reported():
    """xyz1 and xyz10 must both survive to be probed."""
    pkgs = ["definitely-not-a-real-pkg-xyz1", "definitely-not-a-real-pkg-xyz10"]
    result = probe_all(pkgs)
    named = {f["package"] for f in result["failures"]}
    assert named == set(pkgs), named


def test_truncated_is_false_when_every_failure_fits():
    result = probe_all(["definitely-not-a-real-pkg-aaa", "definitely-not-a-real-pkg-bbb"])
    assert result["truncated"] is False
    assert len(result["failures"]) == 2


def test_truncated_is_true_when_the_round_limit_is_hit():
    """A partial failure list must not be presented as complete."""
    pkgs = [f"definitely-not-a-real-pkg-t{i:02d}" for i in range(8)]
    result = probe_all(pkgs, max_rounds=3)
    assert result["truncated"] is True
    assert len(result["failures"]) == 3, result["failures"]


def test_a_clean_resolve_is_never_truncated():
    result = probe_all(["certifi"])
    assert result["ok"] is True
    assert result["truncated"] is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} runner-error tests passed")
