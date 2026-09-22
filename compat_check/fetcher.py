"""Resolve a GitHub repo URL or a PyPI package name into requirement specs.

GitHub path tries pyproject.toml, then requirements.txt, then setup.cfg,
then setup.py, on raw.githubusercontent.com — unauthenticated, no rate limit
(confirmed in prior research). setup.py is read by AST (setuppy.py) and is
tried last, because the other three are declarative and always more reliable
than reading code without running it.

The branch is resolved via api.github.com rather than guessed. That API IS
rate limited (60/hour unauthenticated, measured), so every failure mode of
the lookup degrades to the old main/master probing instead of erroring.

requirements.txt is parsed by reqline.py and its -r/-c/-e references are
followed by includes.py, so the returned list is complete or the call raises.
Measured on home-assistant/core: 47 requirements before following includes,
181 after — the earlier code silently reported the 47 as the whole set.

PyPI path reads info.requires_dist from the package's JSON API.

fetch_requirements() distinguishes "found nothing" from "found an empty
list" by raising FetchError instead of ever returning []. Callers must not
confuse an empty return with runner.probe_all([]), which is a "no
requirements, trivially ok" result — this module never emits an empty list.
"""
from __future__ import annotations

import configparser
import json
import re
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass

from compat_check.includes import IncludeError, resolve_includes
from compat_check.reqline import parse_lines
from compat_check.setuppy import SetupPyError, parse_setup_py


class FetchError(Exception):
    """Raised when a source could not be resolved to any requirements."""


@dataclass
class GitHubRef:
    owner: str
    repo: str
    branch: str | None  # None = try main, then master


# https://github.com/o/r, optionally /tree/<branch> or /blob/<branch>/<path...>.
# The trailing path of a /blob/ deep link is captured and discarded: the branch
# is what we need, the file is not.
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/(?P<branch>[^/]+)(?:/.*)?)?/?$"
)

# github.com/o/r with no scheme — a form people paste constantly. Without this
# the string fell through to the PyPI lookup and failed with
# "PyPI package not found: github.com/psf/requests".
_GITHUB_BARE_RE = re.compile(
    r"^(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/(?P<branch>[^/]+)(?:/.*)?)?/?$"
)

# git@github.com:owner/repo.git — the SSH clone form.
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

_GITHUB_FORMS = (_GITHUB_URL_RE, _GITHUB_BARE_RE, _GITHUB_SSH_RE)

# setup.py is last on purpose: pyproject/requirements/setup.cfg are
# declarative, while setup.py is read by AST and may legitimately be
# unreadable (a computed install_requires).
_CANDIDATE_FILES = ("pyproject.toml", "requirements.txt", "setup.cfg", "setup.py")
_BRANCH_FALLBACKS = ("main", "master")
_USER_AGENT = "compat-check/0.1 (+https://github.com/)"
_REQUEST_TIMEOUT = 15


class RepoLookup:
    """Outcome of asking the GitHub API about a repo.

    `default_branch` is None when the API could not answer, in which case the
    caller falls back to probing main/master. The API is an optimization and a
    diagnostic, never a hard dependency: unauthenticated callers get 60
    requests per hour (measured), so exhausting it must degrade, not fail.
    """

    def __init__(self, default_branch: str | None = None, problem: str | None = None):
        self.default_branch = default_branch
        self.problem = problem


def _lookup_repo(ref: GitHubRef) -> RepoLookup:
    """Resolve the default branch, and diagnose the failure when there isn't one.

    Replaces guessing main-then-master. The guess wasted a 404 round-trip per
    candidate file on any repo defaulting to something else (measured on this
    tool's own repo, which defaults to master), and collapsed several distinct
    failures into one indistinguishable FetchError.
    """
    url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return RepoLookup(default_branch=data.get("default_branch"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # GitHub deliberately returns 404 for a private repo as well as a
            # missing one, so it does not leak which private repos exist.
            # Verified: github/github and a made-up name both return 404.
            return RepoLookup(problem=(
                f"GitHub has no public repository {ref.owner}/{ref.repo}. "
                f"It either does not exist or is private — GitHub returns the "
                f"same 404 for both, so this tool cannot tell them apart. "
                f"Private repositories are not supported."
            ))
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = e.headers.get("X-RateLimit-Reset", "")
            when = ""
            if reset.isdigit():
                when = f" (resets at {time.strftime('%H:%M:%S', time.localtime(int(reset)))})"
            return RepoLookup(problem=(
                f"GitHub API rate limit exhausted{when}. compat-check makes "
                f"unauthenticated requests, which are limited to 60 per hour."
            ))
        # Any other status: the API is unusable but the raw file host may not
        # be, so fall back rather than failing the whole lookup.
        return RepoLookup()
    except urllib.error.URLError:
        return RepoLookup()


def _http_get(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise FetchError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"network error fetching {url}: {e.reason}") from e


def parse_github_url(source: str) -> GitHubRef | None:
    """Parse any of the GitHub forms people actually paste, else None.

    Returning None routes the source to the PyPI lookup, so a string that is
    obviously a GitHub reference but unparseable must NOT return None —
    see looks_like_github().
    """
    source = source.strip()
    for pattern in _GITHUB_FORMS:
        m = pattern.match(source)
        if m:
            groups = m.groupdict()
            return GitHubRef(
                owner=groups["owner"],
                repo=groups["repo"],
                branch=groups.get("branch"),
            )
    return None


def looks_like_github(source: str) -> bool:
    """True for a string that references GitHub but did not parse.

    Without this check such a string fell through to _fetch_from_pypi() and
    produced "PyPI package not found: github.com/psf/requests" — a message
    about the wrong service entirely.
    """
    return "github.com" in source.lower()


def _parse_pyproject_toml(text: str) -> list[str]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    deps = list(project.get("dependencies", []))
    # optional-dependencies groups are extras, not base requirements — skipped intentionally.
    return deps


def _parse_requirements_txt(text: str) -> list[str]:
    """Parse a standalone requirements.txt with NO include following.

    Retained for callers that have only the text and no way to fetch siblings.
    A file using -r/-c/-e cannot be answered this way — resolve_includes()
    handles those, and _fetch_from_github() uses it. Raises FetchError rather
    than returning a partial list, which is the failure this module exists to
    avoid.
    """
    parsed = parse_lines(text)
    if not parsed.is_complete:
        raise FetchError(
            "requirements.txt references other files "
            f"({', '.join(parsed.includes + parsed.constraints + parsed.editables)}) "
            "and cannot be parsed without fetching them"
        )
    return parsed.requirements


def _parse_setup_cfg(text: str) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read_string(text)
    if not parser.has_option("options", "install_requires"):
        return []
    raw = parser.get("options", "install_requires")
    return [line.strip() for line in raw.splitlines() if line.strip()]


_PARSERS = {
    "pyproject.toml": _parse_pyproject_toml,
    "requirements.txt": _parse_requirements_txt,
    "setup.cfg": _parse_setup_cfg,
    "setup.py": parse_setup_py,
}


def _repo_file_resolver(ref: GitHubRef, branch: str):
    """A Resolver for includes.resolve_includes(), bound to one repo+branch."""
    def resolve(path: str) -> str | None:
        url = f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{branch}/{path}"
        return _http_get(url)
    return resolve


def _fetch_requirements_txt(ref: GitHubRef, branch: str, filename: str, text: str) -> list[str]:
    """Parse requirements.txt AND follow every -r/-c/-e it references.

    Without this, a file whose real content is `-r base.txt` returned only the
    handful of lines that happened to sit beside the include, and the caller
    was told that was the complete set.
    """
    resolved = resolve_includes(filename, text, _repo_file_resolver(ref, branch))
    # resolved.constraints are pins, not things to install — a constraint says
    # "IF this package is pulled in, use this version". Merging them would turn
    # every pin into a requirement (measured on home-assistant/core: 51 real
    # requirements would have become 181). They are deliberately not returned.

    # `-e .` and `-e ./pkg` name a package in this same repo; its real
    # dependencies live in that directory's own pyproject/setup.cfg.
    for target in resolved.local_editables:
        prefix = "" if target in (".", "") else f"{target.rstrip('/')}/"
        # setup.py included: records/records is exactly this shape — a
        # requirements.txt of "-e .[pg]" plus a setup.py and nothing else.
        # Without it the real dependencies were invisible and only the one
        # sibling line survived.
        for candidate in ("pyproject.toml", "setup.cfg", "setup.py"):
            sub = _http_get(
                f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/"
                f"{branch}/{prefix}{candidate}"
            )
            if sub is None:
                continue
            try:
                resolved.requirements.extend(_PARSERS[candidate](sub))
            except SetupPyError:
                # This candidate exists but is not statically readable; a
                # later one might be. Only if none works do we lose the deps.
                continue
            except Exception as e:
                raise FetchError(f"failed to parse {prefix}{candidate} for -e {target}: {e}") from e
            break

    seen: set[str] = set()
    deduped = []
    for req in resolved.requirements:
        if req not in seen:
            seen.add(req)
            deduped.append(req)
    return deduped


def _fetch_from_github(ref: GitHubRef) -> list[str]:
    if ref.branch:
        branches = [ref.branch]
        lookup = RepoLookup()
    else:
        lookup = _lookup_repo(ref)
        if lookup.problem:
            # A diagnosed failure is final: probing raw.githubusercontent.com
            # would only produce 404s and a vaguer message.
            raise FetchError(lookup.problem)
        if lookup.default_branch:
            branches = [lookup.default_branch]
        else:
            # API unreachable or unhelpful — degrade to the old guess rather
            # than failing on what is only an optimization.
            branches = list(_BRANCH_FALLBACKS)

    tried = []
    setup_py_problem: str | None = None
    for branch in branches:
        for filename in _CANDIDATE_FILES:
            url = f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{branch}/{filename}"
            tried.append(url)
            text = _http_get(url)
            if text is None:
                continue
            try:
                if filename == "requirements.txt":
                    reqs = _fetch_requirements_txt(ref, branch, filename, text)
                else:
                    reqs = _PARSERS[filename](text)
            except SetupPyError as e:
                # Not fatal on its own: this is the last candidate, and the
                # repo may simply compute its requirements. Remember why, so
                # the final error explains it instead of saying "not found".
                setup_py_problem = str(e)
                continue
            except IncludeError as e:
                # An unresolvable include means the requirement set is unknown.
                # Failing loudly is the whole point — do not fall through to
                # another candidate file and report a different, partial answer.
                raise FetchError(f"{url}: {e}") from e
            except Exception as e:
                raise FetchError(f"failed to parse {url}: {e}") from e
            if reqs:
                return reqs
            # file exists but declares no dependencies — keep searching other files/branches
    branch_note = (
        f" on branch {branches[0]}" if len(branches) == 1
        else f" on any of {', '.join(branches)}"
    )
    if setup_py_problem:
        raise FetchError(
            f"{ref.owner}/{ref.repo}{branch_note}: the only dependency "
            f"declaration is a setup.py that cannot be read statically — "
            f"{setup_py_problem}"
        )
    raise FetchError(
        f"no pyproject.toml/requirements.txt/setup.cfg/setup.py with dependencies "
        f"found for {ref.owner}/{ref.repo}{branch_note} (tried: {', '.join(tried)})"
    )


def _fetch_from_pypi(package_name: str) -> list[str]:
    url = f"https://pypi.org/pypi/{package_name}/json"
    text = _http_get(url)
    if text is None:
        raise FetchError(f"PyPI package not found: {package_name}")
    data = json.loads(text)
    requires_dist = data.get("info", {}).get("requires_dist") or []
    # drop extras-only markers (e.g. 'foo; extra == "dev"') — base install only.
    base = [r for r in requires_dist if "extra ==" not in r]
    if not base:
        raise FetchError(f"PyPI package {package_name} declares no base requires_dist")
    return base


def fetch_requirements(source: str) -> list[str]:
    """Resolve `source` (GitHub URL or PyPI package name) to requirement specs.

    Raises FetchError if nothing could be resolved. Never returns [] —
    that return value is reserved for callers of runner.probe_all() to mean
    "no requirements, trivially ok", which this function must not be
    confused with.
    """
    ref = parse_github_url(source)
    if ref is not None:
        return _fetch_from_github(ref)
    if looks_like_github(source):
        # Do not fall through to PyPI: the user clearly meant GitHub, and a
        # "PyPI package not found" message would send them looking in the
        # wrong place entirely.
        raise FetchError(
            f"{source!r} looks like a GitHub reference but could not be parsed. "
            f"Expected forms: https://github.com/<owner>/<repo>, "
            f"github.com/<owner>/<repo>, git@github.com:<owner>/<repo>.git, "
            f"optionally with /tree/<branch>."
        )
    return _fetch_from_pypi(source)
