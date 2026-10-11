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
    normalize_name,
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


# --- Transitive wheel gaps, captured from `uv 0.12.24` against Python 3.15.0rc3
# on 2026-10-11 (uv pip install --dry-run). The failing package used to come
# back <unknown> for botorch/torchdiffeq, which stopped probe_all()'s retry loop.

# A package whose own requirement is the blocker.
_ABI_DIRECT = """\
error: No solution found when resolving dependencies
  cause: Because all versions of torch have no wheels with a matching Python ABI tag (e.g., `cp315`) and you require torch, we can conclude that your requirements are unsatisfiable.

hint: You require CPython 3.15 (`cp315`), but we only found wheels for `torch` (v2.14.1) with the following Python ABI tags: `cp310`, `cp311`, `cp312`, `cp313`, `cp314`, `cp314t`
"""

# Chain: markitdown -> magika -> onnxruntime (no cp315 wheel). The root cause
# is onnxruntime; the requirement to drop is markitdown.
_ABI_CHAIN = """\
error: No solution found when resolving dependencies
  cause: Because onnxruntime>=1.17.0 has no wheels with a matching Python ABI tag (e.g., `cp315`) and magika>=0.6.1,<=0.6.3 depends on onnxruntime>=1.17.0, we can conclude that magika>=0.6.1,<=0.6.3 cannot be used.
         And because only the following versions of magika are available:
             magika<=0.6.3
             magika>0.7.dev0
         we can conclude that magika>=0.6.1,<0.7.dev0 cannot be used.
         And because markitdown>=0.1.0 depends on magika>=0.6.1,<0.7.dev0 and you require markitdown>=0.1, we can conclude that your requirements are unsatisfiable.

hint: You require CPython 3.15 (`cp315`), but we only found wheels for `onnxruntime` (v1.31.0) with the following Python ABI tags: `cp311`, `cp312`, `cp313`, `cp313t`, `cp314`, `cp314t`
"""

# botorch: the dropped requirement is the version-bounded "torch>=2.4".
_ABI_BOUNDED = """\
error: No solution found when resolving dependencies
  cause: Because torch>=2.4.0 has no wheels with a matching Python ABI tag (e.g., `cp315`) and you require torch>=2.4, we can conclude that your requirements are unsatisfiable.

hint: You require CPython 3.15 (`cp315`), but we only found wheels for `torch` (v2.14.1) with the following Python ABI tags: `cp310`, `cp311`, `cp312`, `cp313`, `cp314`, `cp314t`
"""

# linear-operator -> torch, with the "And because you require" continuation form.
_ABI_AND_REQUIRE = """\
error: No solution found when resolving dependencies
  cause: Because torch>=2.0.0 has no wheels with a matching Python ABI tag (e.g., `cp315`) and linear-operator>=0.6.1 depends on torch>=2.0, we can conclude that linear-operator>=0.6.1 cannot be used.
         And because you require linear-operator>=0.6.1, we can conclude that your requirements are unsatisfiable.

hint: You require CPython 3.15 (`cp315`), but we only found wheels for `torch` (v2.14.1) with the following Python ABI tags: `cp310`, `cp311`, `cp312`, `cp313`, `cp314`, `cp314t`
"""

# Older uv wording: version in parentheses, no "(e.g., ...)" tag.
_ABI_OLD_FORM = """\
  \u00d7 No solution found when resolving dependencies:
  \u2570\u2500\u25b6 Because onnxruntime (v1.17.0) has no wheels with a matching Python ABI tag
      and you require onnxruntime, we can conclude that your requirements are
      unsatisfiable.
"""


def test_abi_direct_requirement_is_dropped_and_is_its_own_root_cause():
    d = _UV._extract_detail(_ABI_DIRECT)
    assert d.candidates[0] == "torch"
    assert d.root_package == "torch"
    assert d.root_reason == "no cp315 wheel"


def test_abi_chain_drops_the_direct_requirement_but_names_the_root_cause():
    d = _UV._extract_detail(_ABI_CHAIN)
    assert d.candidates[0] == "markitdown", d.candidates
    assert d.root_package == "onnxruntime"
    assert d.root_reason == "no cp315 wheel"
    assert _UV._extract_failing_package(_ABI_CHAIN) == "markitdown"


def test_abi_bounded_requirement_is_attributed_to_torch():
    """botorch came back <unknown> before: the 'torch>=2.4.0' form has no (v...)."""
    assert _UV._extract_failing_package(_ABI_BOUNDED) == "torch"
    assert _UV._extract_detail(_ABI_BOUNDED).root_package == "torch"


def test_and_you_require_continuation_form_drops_the_direct_requirement():
    d = _UV._extract_detail(_ABI_AND_REQUIRE)
    assert d.candidates[0] == "linear-operator", d.candidates
    assert d.root_package == "torch"


def test_old_parenthesised_wording_still_matches():
    d = _UV._extract_detail(_ABI_OLD_FORM)
    assert d.root_package == "onnxruntime"
    assert d.candidates[0] == "onnxruntime"
    # No "(e.g., `cpXYZ`)" in this wording, so the tag is not invented.
    assert d.root_reason == "no wheels with a matching Python ABI tag"


def test_hint_line_is_parsed_into_available_tags():
    d = _UV._extract_detail(_ABI_CHAIN)
    assert d.available_tags == ["cp311", "cp312", "cp313", "cp313t", "cp314", "cp314t"]
    assert d.tags_for == "onnxruntime v1.31.0"
    assert d.hint is not None and d.hint.startswith("You require CPython 3.15")


def test_hint_line_is_not_mistaken_for_a_requirement():
    """'You require CPython 3.15' must never make CPython a drop candidate."""
    for text in (_ABI_DIRECT, _ABI_CHAIN, _ABI_BOUNDED, _ABI_AND_REQUIRE):
        assert "CPython" not in _UV._extract_detail(text).candidates


def test_no_hint_means_no_tags():
    d = _UV._extract_detail(_CONFLICT)
    assert d.hint is None and d.available_tags == []
    assert d.root_package is None


def test_not_found_with_a_transitive_cause_still_drops_the_direct_requirement():
    text = (
        "  cause: Because ghost-dep was not found in the package registry and "
        "my-app==1.0 depends on ghost-dep, we can conclude that my-app==1.0 "
        "cannot be used.\n         And because you require my-app==1.0, we "
        "can conclude that your requirements are unsatisfiable.\n"
    )
    d = _UV._extract_detail(text)
    assert d.candidates[:2] == ["my-app", "ghost-dep"], d.candidates
    assert d.root_package == "ghost-dep"
    assert d.root_reason == "not found in the package registry"


def test_normalize_name_is_pep503():
    assert normalize_name("Ruamel.YAML") == "ruamel-yaml"
    assert normalize_name("charset_normalizer") == "charset-normalizer"
    assert _drops("ruamel.yaml>=0.17", "ruamel-yaml") is True


def test_probe_all_continues_past_a_transitive_failure():
    """The retry loop must drop the direct requirement, not stall on the root cause.

    Offline: the backend is replaced by one that replays the captured chain.
    """
    from unittest import mock
    from compat_check import runner

    class _Replay(_UvBackend):
        def create_venv(self, venv_path, python_version):
            import subprocess
            return subprocess.CompletedProcess([], 0, "", "")

        def dry_run_install(self, venv_path, requirements, constraint_file=None):
            import subprocess
            names = [runner._requirement_name(r) for r in requirements]
            if "markitdown" in names:
                return subprocess.CompletedProcess([], 1, "", _ABI_CHAIN)
            if "torch" in names:
                return subprocess.CompletedProcess([], 1, "", _ABI_BOUNDED)
            return subprocess.CompletedProcess([], 0, "", "")

    with mock.patch.object(runner, "_select_backend", return_value=_Replay()):
        result = probe_all(["markitdown>=0.1", "torch>=2.4", "certifi"])
    pkgs = [f["package"] for f in result["failures"]]
    assert pkgs == ["markitdown", "torch"], pkgs
    first = result["failures"][0]
    assert first["package"] == "markitdown"
    assert first["root_package"] == "onnxruntime"
    assert first["available_tags"][0] == "cp311"
    assert result["failures"][1]["root_package"] == "torch"
    assert result["truncated"] is False


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
