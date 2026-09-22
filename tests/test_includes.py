"""Tests for compat_check.includes — following -r / -c / -e references.

The central case is test_diamond_include_resolves: it is the one a shared
`seen` set gets wrong, which is why this module copies pip's per-branch dict
instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.includes import IncludeError, resolve_includes


def fake_resolver(files: dict):
    return lambda path: files.get(path)


def test_simple_include_is_followed():
    files = {"requirements.txt": "-r base.txt\ntop\n", "base.txt": "requests>=2.0\n"}
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert got.requirements == ["top", "requests>=2.0"]
    assert got.files_read == ["requirements.txt", "base.txt"]


def test_diamond_include_resolves_rather_than_raising():
    """A shared `seen` set cannot tell this from a cycle. pip's dict can."""
    files = {
        "requirements.txt": "-r base.txt\n-r test.txt\ntop\n",
        "base.txt": "requests>=2.0\n",
        "test.txt": "-r base.txt\npytest\n",
    }
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert got.requirements == ["top", "requests>=2.0", "pytest"]
    # base.txt legitimately read twice; its requirement appears once.
    assert got.files_read.count("base.txt") == 2
    assert got.requirements.count("requests>=2.0") == 1


def test_true_cycle_raises():
    files = {"a.txt": "-r b.txt\nx\n", "b.txt": "-r a.txt\ny\n"}
    try:
        resolve_includes("a.txt", files["a.txt"], fake_resolver(files))
    except IncludeError as e:
        assert "recursively" in str(e)
        return
    raise AssertionError("a cycle must raise")


def test_self_reference_raises():
    files = {"a.txt": "-r a.txt\nx\n"}
    try:
        resolve_includes("a.txt", files["a.txt"], fake_resolver(files))
    except IncludeError:
        return
    raise AssertionError("a self-reference must raise")


def test_missing_include_is_fatal_not_silently_dropped():
    """The whole point: never hand back a partial list."""
    files = {"requirements.txt": "-r gone.txt\nrequests\n"}
    try:
        resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    except IncludeError as e:
        assert "gone.txt" in str(e)
        assert "unknown" in str(e)
        return
    raise AssertionError("a missing include must raise, not return ['requests']")


def test_relative_paths_resolve_against_the_including_file():
    files = {
        "requirements/dev.txt": "-r ../base.txt\n-r ./extra.txt\ndev-only\n",
        "base.txt": "requests\n",
        "requirements/extra.txt": "extra-pkg\n",
    }
    got = resolve_includes("requirements/dev.txt", files["requirements/dev.txt"],
                            fake_resolver(files))
    assert got.requirements == ["dev-only", "requests", "extra-pkg"]


def test_depth_cap_trips():
    files = {f"f{i}.txt": f"-r f{i+1}.txt\npkg{i}\n" for i in range(12)}
    files["f12.txt"] = "last\n"
    try:
        resolve_includes("f0.txt", files["f0.txt"], fake_resolver(files))
    except IncludeError as e:
        assert "depth" in str(e)
        return
    raise AssertionError("depth cap must trip")


def test_file_cap_trips():
    files = {"root.txt": "".join(f"-r f{i}.txt\n" for i in range(40))}
    for i in range(40):
        files[f"f{i}.txt"] = f"pkg{i}\n"
    try:
        resolve_includes("root.txt", files["root.txt"], fake_resolver(files))
    except IncludeError as e:
        assert "more than" in str(e)
        return
    raise AssertionError("file cap must trip")


def test_constraints_are_followed_but_kept_out_of_requirements():
    """A -c entry pins IF a package is pulled in; it is not an install target.

    Merging them inflated home-assistant/core from 51 real requirements to 181,
    130 of which existed only as constraints — a wrong answer in the opposite
    direction from the truncation this module fixes.
    """
    files = {"requirements.txt": "-c pins.txt\nrequests\n", "pins.txt": "urllib3==2.0.0\n"}
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert got.requirements == ["requests"]
    assert got.constraints == ["urllib3==2.0.0"]
    assert "pins.txt" in got.files_read


def test_constraint_file_reached_through_an_include_stays_a_constraint():
    files = {
        "requirements.txt": "-r sub.txt\ntop\n",
        "sub.txt": "-c pins.txt\nsubreq\n",
        "pins.txt": "pinned==1.0\n",
    }
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert got.requirements == ["top", "subreq"]
    assert got.constraints == ["pinned==1.0"]


def test_include_nested_inside_a_constraint_file_stays_a_constraint():
    """Once inside a -c file everything below it constrains; being reached
    through -r cannot turn it back into a requirement."""
    files = {
        "requirements.txt": "-c pins.txt\nreq\n",
        "pins.txt": "-r more_pins.txt\npinned==1.0\n",
        "more_pins.txt": "also-pinned==2.0\n",
    }
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert got.requirements == ["req"]
    assert got.constraints == ["pinned==1.0", "also-pinned==2.0"]


def test_local_editable_is_reported_external_one_is_skipped():
    files = {"requirements.txt": "-e .\n-e ./sub\n-e git+https://example.com/o.git\nreq\n"}
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert got.requirements == ["req"]
    assert got.local_editables == [(".", []), ("sub", [])]
    assert any("not followed" in s for s in got.skipped)


def test_editable_extras_are_stripped_from_the_path():
    """`-e .[pg]` targets the repo root, not a directory called `.[pg]`.

    records/records is exactly this shape; leaving the extras in place made the
    resolver look for `.[pg]/pyproject.toml` and lose the real dependencies.
    """
    files = {"requirements.txt": "-e .[pg]\n-e ./sub[extra]\npytest\n"}
    got = resolve_includes("requirements.txt", files["requirements.txt"],
                            fake_resolver(files))
    # Extras are carried alongside the path, not discarded: records' `[pg]`
    # really does require psycopg2-binary, and the caller resolves it.
    assert got.local_editables == [(".", ["pg"]), ("sub", ["extra"])], got.local_editables
    assert got.requirements == ["pytest"]
    assert got.skipped == []


def test_editable_without_extras_produces_no_skip_note():
    files = {"requirements.txt": "-e .\npytest\n"}
    got = resolve_includes("requirements.txt", files["requirements.txt"],
                            fake_resolver(files))
    assert got.local_editables == [(".", [])]
    assert got.skipped == []


def test_skipped_lines_carry_their_source_file():
    files = {
        "requirements.txt": "-r sub.txt\nreq\n",
        "sub.txt": "--index-url https://${TOKEN}@x/simple\nother\n",
    }
    got = resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    assert any(s.startswith("sub.txt:") for s in got.skipped), got.skipped


def test_malformed_option_in_an_included_file_names_that_file():
    files = {"requirements.txt": "-r bad.txt\nreq\n", "bad.txt": "-r\n"}
    try:
        resolve_includes("requirements.txt", files["requirements.txt"], fake_resolver(files))
    except IncludeError as e:
        assert "bad.txt" in str(e)
        return
    raise AssertionError("a malformed option must raise and name its file")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} include tests passed")
