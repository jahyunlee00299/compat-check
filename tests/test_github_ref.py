"""Tests for URL parsing, default-branch detection and error diagnosis (Unit 3).

Network-touching cases are mocked so the suite does not spend the
unauthenticated GitHub API budget (measured: 60 requests/hour).
"""
import sys
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import compat_check.fetcher as fetcher
from compat_check.fetcher import (
    FetchError,
    GitHubRef,
    _lookup_repo,
    fetch_requirements,
    looks_like_github,
    parse_github_url,
)


def test_all_pasted_github_forms_parse():
    cases = {
        "https://github.com/psf/requests": (None,),
        "http://github.com/psf/requests": (None,),
        "https://github.com/psf/requests/": (None,),
        "https://github.com/psf/requests.git": (None,),
        "https://www.github.com/psf/requests": (None,),
        "github.com/psf/requests": (None,),
        "git@github.com:psf/requests.git": (None,),
        "git@github.com:psf/requests": (None,),
        "https://github.com/psf/requests/tree/main": ("main",),
        "https://github.com/psf/requests/blob/dev/setup.py": ("dev",),
        "https://github.com/psf/requests/tree/feature/x": ("feature",),
    }
    for source, (branch,) in cases.items():
        ref = parse_github_url(source)
        assert ref is not None, f"{source!r} did not parse"
        assert ref.owner == "psf", (source, ref)
        assert ref.repo == "requests", (source, ref)
        assert ref.branch == branch, (source, ref)


def test_non_github_sources_return_none():
    for source in ["requests", "https://gitlab.com/o/r", "https://example.com/x"]:
        assert parse_github_url(source) is None


def test_looks_like_github_catches_unparseable_github_strings():
    assert looks_like_github("github.com/not-a-valid-form") is True
    assert looks_like_github("https://GITHUB.COM/weird") is True
    assert looks_like_github("requests") is False


def test_unparseable_github_string_does_not_become_a_pypi_lookup():
    """0.3.0 answered 'PyPI package not found: github.com/psf/requests'."""
    with mock.patch.object(fetcher, "_fetch_from_pypi") as pypi:
        try:
            fetch_requirements("github.com/only-one-segment")
        except FetchError as e:
            assert "GitHub" in str(e)
            assert pypi.call_count == 0, "fell through to the PyPI path"
            return
    raise AssertionError("should have raised a GitHub-specific error")


def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.github.com/repos/o/r", code, "err", headers or {}, None,
    )


def test_lookup_returns_the_real_default_branch():
    payload = b'{"default_branch": "develop"}'
    fake = mock.MagicMock()
    fake.__enter__.return_value.read.return_value = payload
    with mock.patch.object(fetcher.urllib.request, "urlopen", return_value=fake):
        got = _lookup_repo(GitHubRef("o", "r", None))
    assert got.default_branch == "develop"
    assert got.problem is None


def test_404_says_private_or_missing_without_claiming_which():
    """GitHub returns 404 for both; verified against github/github."""
    with mock.patch.object(fetcher.urllib.request, "urlopen",
                            side_effect=_http_error(404)):
        got = _lookup_repo(GitHubRef("o", "r", None))
    assert got.default_branch is None
    assert "private" in got.problem.lower()
    assert "does not exist" in got.problem.lower()


def test_rate_limit_is_reported_as_such():
    err = _http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000000"})
    with mock.patch.object(fetcher.urllib.request, "urlopen", side_effect=err):
        got = _lookup_repo(GitHubRef("o", "r", None))
    assert "rate limit" in got.problem.lower()
    assert "60" in got.problem


def test_a_403_that_is_not_rate_limiting_falls_back_instead_of_diagnosing():
    err = _http_error(403, {"X-RateLimit-Remaining": "42"})
    with mock.patch.object(fetcher.urllib.request, "urlopen", side_effect=err):
        got = _lookup_repo(GitHubRef("o", "r", None))
    assert got.problem is None
    assert got.default_branch is None


def test_network_failure_degrades_to_the_old_branch_guessing():
    """The API is an optimization, never a new hard dependency."""
    with mock.patch.object(fetcher.urllib.request, "urlopen",
                            side_effect=urllib.error.URLError("down")):
        got = _lookup_repo(GitHubRef("o", "r", None))
    assert got.problem is None
    assert got.default_branch is None

    calls = []

    def fake_get(url):
        calls.append(url)
        return "[project]\ndependencies = ['requests']\n" if "master" in url else None

    with mock.patch.object(fetcher, "_lookup_repo", return_value=fetcher.RepoLookup()), \
         mock.patch.object(fetcher, "_http_get", side_effect=fake_get):
        reqs = fetcher._fetch_from_github(GitHubRef("o", "r", None))
    assert reqs == ["requests"]
    assert any("/main/" in c for c in calls), "should still try main first"
    assert any("/master/" in c for c in calls), "should still fall back to master"


def test_explicit_branch_skips_the_api_entirely():
    with mock.patch.object(fetcher, "_lookup_repo") as lookup, \
         mock.patch.object(fetcher, "_http_get",
                            return_value="[project]\ndependencies = ['x']\n"):
        fetcher._fetch_from_github(GitHubRef("o", "r", "custom-branch"))
    assert lookup.call_count == 0, "an explicit branch needs no lookup"


def test_detected_branch_is_the_only_one_probed():
    """The saving: no wasted 404s against a branch that does not exist."""
    calls = []

    def fake_get(url):
        calls.append(url)
        return "[project]\ndependencies = ['x']\n" if "pyproject" in url else None

    with mock.patch.object(fetcher, "_lookup_repo",
                            return_value=fetcher.RepoLookup(default_branch="master")), \
         mock.patch.object(fetcher, "_http_get", side_effect=fake_get):
        fetcher._fetch_from_github(GitHubRef("o", "r", None))
    assert calls and all("/master/" in c for c in calls), calls
    assert not any("/main/" in c for c in calls)


def test_diagnosed_problem_is_final_and_not_followed_by_raw_probing():
    problem = fetcher.RepoLookup(problem="no such repo")
    with mock.patch.object(fetcher, "_lookup_repo", return_value=problem), \
         mock.patch.object(fetcher, "_http_get") as get:
        try:
            fetcher._fetch_from_github(GitHubRef("o", "r", None))
        except FetchError as e:
            assert str(e) == "no such repo"
            assert get.call_count == 0, "should not probe raw files after a diagnosis"
            return
    raise AssertionError("a diagnosed problem must raise")


def test_not_found_error_names_the_branch_searched():
    with mock.patch.object(fetcher, "_lookup_repo",
                            return_value=fetcher.RepoLookup(default_branch="trunk")), \
         mock.patch.object(fetcher, "_http_get", return_value=None):
        try:
            fetcher._fetch_from_github(GitHubRef("o", "r", None))
        except FetchError as e:
            assert "trunk" in str(e)
            return
    raise AssertionError("should raise")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} github-ref tests passed")
