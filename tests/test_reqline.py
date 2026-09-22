"""Tests for compat_check.reqline — requirement-line parsing.

Every case here was a real defect in 0.2.0, found by benchmarking against
pip's req_file.py. All three reached the resolver and were reported to the
user as a failure of package <unknown> in *their* repo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.reqline import (
    env_vars_in,
    join_continuations,
    parse_lines,
    strip_comment,
)


def test_backslash_continuation_is_joined():
    # 0.2.0 produced ['requests \\', '>=2.0'] — two malformed specs.
    result = parse_lines("requests \\\n    >=2.0\nflask\n")
    assert len(result.requirements) == 2, result.requirements
    joined = result.requirements[0]
    assert joined.startswith("requests")
    assert ">=2.0" in joined
    assert "\\" not in joined


def test_continuation_at_end_of_file_is_not_lost():
    result = parse_lines("requests \\\n")
    assert result.requirements == ["requests "] or result.requirements == ["requests"]


def test_commented_continuation_does_not_swallow_next_line():
    # '# disabled \' must not absorb the requests line.
    result = parse_lines("# disabled \\\nrequests\nflask\n")
    assert result.requirements == ["requests", "flask"]


def test_trailing_comment_is_stripped():
    # 0.2.0 passed 'requests>=2.0  # pinned' to uv, which answered
    # "Failed to parse".
    result = parse_lines("requests>=2.0  # pinned for security\nflask # web\n")
    assert result.requirements == ["requests>=2.0", "flask"]


def test_egg_fragment_is_not_mistaken_for_a_comment():
    url = "git+https://example.com/x.git#egg=mypkg"
    assert strip_comment(url) == url
    assert parse_lines(url + "\n").requirements == [url]


def test_whole_line_comments_and_blanks_skipped():
    assert parse_lines("# a\n\n   \n# b\nrequests\n").requirements == ["requests"]


def test_short_options_do_not_leak_as_requirements():
    """-c, -i and -f slipped past 0.2.0's prefix filter and became packages."""
    text = "-c constraints.txt\n-i https://x\n-f https://z\nrequests\n"
    result = parse_lines(text)
    assert result.requirements == ["requests"]
    assert result.constraints == ["constraints.txt"]
    assert not any(r.startswith("-") for r in result.requirements)


def test_every_known_option_is_classified_not_passed_through():
    options = [
        "-r a.txt", "--requirement b.txt",
        "-c c.txt", "--constraint d.txt",
        "-e .", "--editable ./pkg",
        "-i https://x", "--index-url https://x", "--extra-index-url https://y",
        "-f https://z", "--find-links https://z",
        "--no-binary :all:", "--only-binary :all:",
        "--pre", "--prefer-binary", "--require-hashes",
        "--trusted-host h", "--use-feature x", "--no-index",
    ]
    result = parse_lines("\n".join(options) + "\nrequests\n")
    assert result.requirements == ["requests"], result.requirements


def test_equals_form_option_is_parsed():
    result = parse_lines("--requirement=nested.txt\nrequests\n")
    assert result.includes == ["nested.txt"]
    assert result.requirements == ["requests"]


def test_unknown_option_is_skipped_not_treated_as_a_package():
    result = parse_lines("--some-future-flag value\nrequests\n")
    assert result.requirements == ["requests"]
    assert any("unrecognised" in s for s in result.skipped)


def test_env_var_line_is_recorded_and_never_expanded():
    """A requirements.txt can embed a credential; we must not expand ours."""
    line = "--index-url https://${TOKEN}@pypi.example.com/simple"
    result = parse_lines(f"requests\n{line}\n")
    assert result.requirements == ["requests"]
    assert len(result.skipped) == 1
    assert "TOKEN" in result.skipped[0]
    assert "not expanded" in result.skipped[0]


def test_env_vars_in_only_matches_braced_form():
    assert env_vars_in("${TOKEN}") == ["TOKEN"]
    assert env_vars_in("$TOKEN") == []          # pypa/pip#3514: braced form only
    assert env_vars_in("pkg>=1.0") == []


def test_is_complete_reflects_unresolved_references():
    assert parse_lines("requests\n").is_complete is True
    assert parse_lines("-r base.txt\n").is_complete is False
    assert parse_lines("-c pins.txt\n").is_complete is False
    assert parse_lines("-e .\n").is_complete is False


def test_option_without_a_value_raises():
    for bad in ["-r", "--requirement", "-c", "-e"]:
        try:
            parse_lines(bad + "\n")
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} without a value should raise")


def test_join_continuations_is_independently_correct():
    assert join_continuations("a \\\nb\nc\n") == ["a b", "c"]
    assert join_continuations("a\nb\n") == ["a", "b"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} reqline tests passed")
