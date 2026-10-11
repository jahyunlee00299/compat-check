"""Run a dry-run install against a disposable venv and collect failures.

Prefers `uv` when it's on PATH (100x+ faster venv creation, same-process
resolver). Falls back to the standard-library `venv` module + `pip` when
`uv` isn't installed, since most machines this tool will run on don't have
it. Both backends are fail-fast resolvers: a single dry-run call reports
only the first unsatisfiable requirement, so probe_all() drops each failing
package and retries until the resolve succeeds or stops making progress.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from compat_check.params import ResolvedParams, resolve_params


@dataclass
class FailureDetail:
    """What a failed resolve says about WHY, beyond the one name to drop.

    `candidates` are the names probe_all() may drop to make progress, best
    first: the DIRECT requirements the user asked for ("you require X"),
    followed by the package a specific diagnosis names. They are separate
    from `root_package` because the two are different questions — measured on
    2026-10-11 with Python 3.15.0rc3, `markitdown>=0.1` fails because its
    dependency onnxruntime has no cp315 wheel. Dropping `onnxruntime` removes
    nothing from the user's requirement list (the loop then stalls), while
    reporting only `markitdown` hides the actual blocker.
    """
    candidates: list[str] = field(default_factory=list)
    root_package: str | None = None
    root_reason: str | None = None
    hint: str | None = None
    available_tags: list[str] = field(default_factory=list)
    tags_for: str | None = None  # "onnxruntime v1.31.0": whose wheels `available_tags` are


@dataclass
class ProbeResult:
    ok: bool
    stdout: str
    stderr: str
    resolved_packages: list[str] = field(default_factory=list)
    failing_package: str | None = None
    detail: FailureDetail | None = None


class _Backend(ABC):
    name: str

    @abstractmethod
    def create_venv(self, venv_path: Path, python_version: str) -> subprocess.CompletedProcess:
        ...

    @abstractmethod
    def dry_run_install(self, venv_path: Path, requirements: list[str],
                         constraint_file: Path | None = None) -> subprocess.CompletedProcess:
        ...

    @abstractmethod
    def parse(self, proc: subprocess.CompletedProcess) -> ProbeResult:
        ...


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # Windows' cp949 console encoding can't decode uv's box-drawing error
    # glyphs (×, ╰, ▶) — found via a live crash, not by inspection.
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


_RESOLVED_TOKEN_RE = re.compile(r"^(?P<name>.+)-(?P<version>\d[^-]*)$")


def _normalize_resolved(token: str) -> str:
    """Render one resolved package as "name==version" whatever the backend.

    pip reports "requests-2.34.2", uv reports "requests==2.34.2". Splitting on
    the LAST hyphen that starts a digit keeps hyphenated names intact
    ("charset-normalizer-3.5.1" -> "charset-normalizer==3.5.1"). A token that
    matches neither shape is passed through unchanged rather than mangled.
    """
    if "==" in token:
        return token
    m = _RESOLVED_TOKEN_RE.match(token)
    if m is None:
        return token
    return f"{m.group('name')}=={m.group('version')}"


class _UvBackend(_Backend):
    name = "uv"

    # uv wraps its diagnosis at ~80 columns, so a package name can be split
    # across lines from the phrase that introduces it. Every pattern below
    # therefore tolerates a newline + indentation wherever a space appears.
    # Wordings captured from real runs; see tests/test_runner_errors.py.
    _NO_VERSION_RE = re.compile(r"no\s+version\s+of\s+([A-Za-z0-9_.\-]+)")
    # "you require X" names a DIRECT requirement — the only kind probe_all()
    # can drop. Matches both "Because you require X" and the continuation form
    # "...depends on Y and you require X" / "And because you require X".
    # Case-sensitive on purpose: uv's hint line starts "You require CPython
    # 3.15", which must not be read as a requirement named "CPython".
    _YOU_REQUIRE_RE = re.compile(r"\byou\s+require\s+([A-Za-z0-9_.\-]+)[><=!~,;@\[\s]")
    # "Because <pkg> was not found in the package registry" — a name that was
    # never published (typo, private package, renamed project). Previously
    # unmatched, so failing_package came back None and probe_all() stopped
    # after one round, hiding every other failure behind "<unknown>".
    _NOT_FOUND_RE = re.compile(
        r"[Bb]ecause\s+([A-Za-z0-9_.\-]+)\s+was\s+not\s+found\s+in\s+the\s+package\s+registry",
    )
    # The root cause of an ABI/platform wheel gap. Three wordings seen so far:
    #   "Because onnxruntime>=1.17.0 has no wheels with a matching Python ABI tag (e.g., `cp315`)"
    #   "Because all versions of torch have no wheels with a matching Python ABI tag (e.g., `cp315`)"
    #   "Because onnxruntime (v1.17.0) has no wheels with a matching Python ABI tag"
    # The optional specifier (">=0.6.1,<=0.6.3") sits between name and verb.
    _NO_WHEELS_RE = re.compile(
        r"[Bb]ecause\s+(?:all\s+versions\s+of\s+)?(?P<name>[A-Za-z0-9_.\-]+)"
        r"(?:\s*\(v[^)]+\)|[<>=!~][^\s,]*(?:,\s*[<>=!~][^\s,]*)*)?"
        r"\s+(?:has|have)\s+no\s+wheels\s+with\s+a\s+matching\s+"
        r"(?P<kind>[A-Za-z]+(?:\s+[A-Za-z]+)?)\s+tag"
        r"(?:\s+\(e\.g\.,\s+`(?P<tag>[^`]+)`\))?",
    )
    # uv's closing hint, e.g.
    #   hint: You require CPython 3.15 (`cp315`), but we only found wheels for
    #   `onnxruntime` (v1.31.0) with the following Python ABI tags: `cp311`, ...
    _HINT_RE = re.compile(r"(?m)^[ \t]*hint:[ \t]*(?P<text>.*(?:\n(?![ \t]*\n)[ \t]+\S.*)*)")
    _HINT_TAGS_RE = re.compile(
        r"found\s+wheels\s+for\s+`(?P<name>[^`]+)`(?:\s+\(v(?P<version>[^)]+)\))?"
        r"\s+with\s+the\s+following\s+(?P<kind>[A-Za-z ]+?)\s+tags:\s*(?P<tags>[^\n]*)",
    )

    def create_venv(self, venv_path: Path, python_version: str) -> subprocess.CompletedProcess:
        return _run(["uv", "venv", str(venv_path), "--python", python_version])

    def dry_run_install(self, venv_path: Path, requirements: list[str],
                         constraint_file: Path | None = None) -> subprocess.CompletedProcess:
        cmd = ["uv", "pip", "install", "--dry-run", "--python", str(venv_path)]
        if constraint_file is not None:
            cmd += ["--constraint", str(constraint_file)]
        return _run([*cmd, *requirements])

    def parse(self, proc: subprocess.CompletedProcess) -> ProbeResult:
        ok = proc.returncode == 0
        resolved = []
        if ok:
            # uv writes the install plan ("+ pkg==ver") to stderr, not stdout.
            resolved = [
                line.strip(" +")
                for line in proc.stderr.splitlines()
                if line.strip().startswith("+")
            ]
        detail = None if ok else self._extract_detail(proc.stderr)
        failing = detail.candidates[0] if detail and detail.candidates else None
        return ProbeResult(ok=ok, stdout=proc.stdout, stderr=proc.stderr,
                            resolved_packages=resolved, failing_package=failing,
                            detail=detail)

    def _extract_failing_package(self, stderr: str) -> str | None:
        detail = self._extract_detail(stderr)
        return detail.candidates[0] if detail.candidates else None

    def _extract_detail(self, stderr: str) -> FailureDetail:
        """Diagnose one failed resolve: what to drop, and what really blocks it.

        Candidate order: direct requirements ("you require X") first, then the
        package a specific diagnosis names. The old order (specific first)
        attributed a transitive failure to a package that is not in the user's
        requirement list; dropping it changed nothing and the retry loop
        stopped with `<unknown>` (botorch, torchdiffeq on Python 3.15).
        """
        # The hint block restates the requirement in prose; keep it out of the
        # sentence-level searches and parse it separately.
        hint_match = self._HINT_RE.search(stderr)
        body = stderr[:hint_match.start()] if hint_match else stderr

        candidates: list[str] = []

        def add(name: str) -> None:
            if name not in candidates:
                candidates.append(name)

        for m in self._YOU_REQUIRE_RE.finditer(body):
            add(m.group(1))

        detail = FailureDetail()
        wheels = self._NO_WHEELS_RE.search(body)
        if wheels:
            detail.root_package = wheels.group("name")
            kind = " ".join(wheels.group("kind").split())
            tag = wheels.group("tag")
            if kind.lower() == "python abi" and tag:
                detail.root_reason = f"no {tag} wheel"
            else:
                detail.root_reason = f"no wheels with a matching {kind} tag" + (
                    f" ({tag})" if tag else "")
            add(detail.root_package)
        for pattern in (self._NOT_FOUND_RE, self._NO_VERSION_RE):
            m = pattern.search(body)
            if m:
                if detail.root_package is None:
                    detail.root_package = m.group(1)
                    detail.root_reason = (
                        "not found in the package registry"
                        if pattern is self._NOT_FOUND_RE else "no matching version")
                add(m.group(1))

        if hint_match:
            text = " ".join(hint_match.group("text").split())
            detail.hint = text
            tags = self._HINT_TAGS_RE.search(text)
            if tags:
                detail.available_tags = re.findall(r"`([^`]+)`", tags.group("tags"))
                detail.tags_for = tags.group("name") + (
                    f" v{tags.group('version')}" if tags.group("version") else "")

        detail.candidates = candidates
        return detail


class _PipBackend(_Backend):
    """Fallback for machines without uv — stdlib venv + pip, same interface."""
    name = "pip"

    _NO_MATCH_RE = re.compile(
        r"Could not find a version that satisfies the requirement ([A-Za-z0-9_.\-]+)",
    )
    _CONFLICT_RE = re.compile(
        r"Cannot install ([A-Za-z0-9_.\-]+)[><=! ].+ and \1", re.IGNORECASE,
    )
    _USER_REQUESTED_RE = re.compile(r"The user requested ([A-Za-z0-9_.\-]+)")

    def create_venv(self, venv_path: Path, python_version: str) -> subprocess.CompletedProcess:
        # python_version is advisory only here — the fallback uses whatever
        # interpreter is running this process; it cannot fetch other versions
        # the way `uv venv --python X.Y` can.
        return _run([sys.executable, "-m", "venv", str(venv_path)])

    def _venv_python(self, venv_path: Path) -> str:
        if sys.platform == "win32":
            return str(venv_path / "Scripts" / "python.exe")
        return str(venv_path / "bin" / "python")

    def dry_run_install(self, venv_path: Path, requirements: list[str],
                         constraint_file: Path | None = None) -> subprocess.CompletedProcess:
        py = self._venv_python(venv_path)
        cmd = [py, "-m", "pip", "install", "--dry-run"]
        if constraint_file is not None:
            cmd += ["--constraint", str(constraint_file)]
        return _run([*cmd, *requirements])

    def parse(self, proc: subprocess.CompletedProcess) -> ProbeResult:
        ok = proc.returncode == 0
        resolved = []
        if ok:
            # pip writes "Would install pkg1-ver pkg2-ver ..." to stdout, using a
            # single hyphen where uv writes "pkg==ver". Normalized here so callers
            # (and the cached rows they compare against) see one format regardless
            # of which backend ran — the mismatch was real: tests asserting
            # "requests==" passed under uv and failed under pip.
            for line in proc.stdout.splitlines():
                if line.startswith("Would install "):
                    resolved = [
                        _normalize_resolved(tok)
                        for tok in line[len("Would install "):].split()
                    ]
        # `python -m pip` routes ERROR: lines to stderr; a standalone `pip`
        # executable was observed routing them to stdout instead — check both.
        failing = None if ok else (
            self._extract_failing_package(proc.stderr) or self._extract_failing_package(proc.stdout)
        )
        return ProbeResult(ok=ok, stdout=proc.stdout, stderr=proc.stderr,
                            resolved_packages=resolved, failing_package=failing)

    def _extract_failing_package(self, stdout: str) -> str | None:
        m = self._NO_MATCH_RE.search(stdout)
        if m:
            return m.group(1)
        # Conflict messages name every requested package; take the first one
        # since we only need *a* package to drop and retry.
        m = self._USER_REQUESTED_RE.search(stdout)
        if m:
            return m.group(1)
        return None


def _select_backend() -> _Backend:
    if shutil.which("uv"):
        return _UvBackend()
    return _PipBackend()


_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)")
_NORMALIZE_RE = re.compile(r"[-_.]+")


def normalize_name(name: str) -> str:
    """PEP 503 normalized distribution name ("Ruamel.YAML" -> "ruamel-yaml")."""
    return _NORMALIZE_RE.sub("-", name.strip().lower())


def _requirement_name(spec: str) -> str:
    """The distribution name at the head of a requirement spec.

    `requirement_matches()` used to drop entries with `startswith(pkg)`, which
    is prefix matching: dropping `pkg1` also dropped `pkg10`..`pkg19`, since
    those names begin with `pkg1`. Measured with 25 distinct unsatisfiable
    packages — the loop finished in 10 rounds having silently discarded 15 of
    them, neither probed nor reported. Names are compared whole instead.
    """
    m = _REQ_NAME_RE.match(spec)
    return normalize_name(m.group(1) if m else spec)


def _drops(spec: str, package: str) -> bool:
    """True when `spec` requests `package` (PEP 503 normalized name match)."""
    return _requirement_name(spec) == normalize_name(package)


def probe_once(requirements: list[str], python_version: str = "3.11",
                backend: _Backend | None = None,
                constraints: list[str] | None = None) -> ProbeResult:
    """Create a throwaway venv and dry-run install the given requirement specs.

    `constraints` are the entries of any `-c` file the source declared. They
    bound versions WITHOUT requesting installation, which is what pip and uv
    both mean by --constraint. Measured: resolving `requests` against
    `urllib3<1.0` yields requests==2.15.1 instead of the latest — ignoring the
    constraint file silently checks a different version set than the project
    itself would install.
    """
    backend = backend or _select_backend()
    with tempfile.TemporaryDirectory(prefix="compat_check_") as tmp:
        venv_path = Path(tmp) / "venv"
        create = backend.create_venv(venv_path, python_version)
        if create.returncode != 0:
            return ProbeResult(ok=False, stdout=create.stdout, stderr=create.stderr)

        if not requirements:
            return ProbeResult(ok=True, stdout="", stderr="")

        constraint_file = None
        if constraints:
            constraint_file = Path(tmp) / "constraints.txt"
            constraint_file.write_text("\n".join(constraints) + "\n", encoding="utf-8")

        install = backend.dry_run_install(venv_path, requirements, constraint_file)
        return backend.parse(install)


def probe_all(requirements: list[str], python_version: str = "3.11", max_rounds: int = 20,
               constraints: list[str] | None = None) -> dict:
    """Repeatedly probe, dropping each failing package, to surface every failure.

    Returns {"ok": bool, "backend": str, "params": ResolvedParams,
    "failures": [{"package": str, "stderr": str, "root_package"?: str,
    "root_reason"?: str, "hint"?: str, "available_tags"?: [str],
    "tags_for"?: str}],
    "resolved": [str], "truncated": bool}. `root_package` is the package
    uv blames when that differs from the requirement that had to be dropped.

    `truncated` is True when `max_rounds` was exhausted with requirements left
    unprobed — the failure list is then a lower bound, not the whole story.

    The python version is resolved through params.resolve_params() before any
    venv is built, so an invalid value fails here rather than inside uv, and a
    value the pip backend cannot honour is reported as ignored instead of being
    passed along as if it applied.
    """
    backend = _select_backend()
    params = resolve_params(python_version, backend.name)
    python_version = params.effective_python
    remaining = list(requirements)
    failures: list[dict] = []
    seen_failing: set[str] = set()

    truncated = False
    for round_number in range(max_rounds):
        result = probe_once(remaining, python_version, backend=backend,
                             constraints=constraints)
        if result.ok:
            return {"ok": not failures, "backend": backend.name, "params": params,
                     "failures": failures, "resolved": result.resolved_packages,
                     "truncated": False}

        combined_output = "\n".join(s for s in (result.stderr, result.stdout) if s)
        detail = result.detail
        # Drop a name the user actually asked for. The name uv blames can be
        # transitive (markitdown -> magika -> onnxruntime); dropping that
        # changes nothing, so take the first candidate present in `remaining`.
        candidates = list(detail.candidates) if detail else (
            [result.failing_package] if result.failing_package else [])
        pkg = next((c for c in candidates
                    if any(_drops(r, c) for r in remaining)), None)
        failure = {"package": pkg or (candidates[0] if candidates else "<unknown>"),
                   "stderr": combined_output}
        if detail is not None and detail.root_package:
            failure["root_package"] = detail.root_package
            failure["root_reason"] = detail.root_reason
        if detail is not None and detail.hint:
            failure["hint"] = detail.hint
            failure["available_tags"] = detail.available_tags
            failure["tags_for"] = detail.tags_for
        if pkg is None or pkg in seen_failing:
            failures.append(failure)
            break

        seen_failing.add(pkg)
        failures.append(failure)
        remaining = [r for r in remaining if not _drops(r, pkg)]
    else:
        # The loop ran out of rounds rather than resolving. Anything still in
        # `remaining` was never probed: reporting only what we found would
        # present a partial list as complete, which is the failure this whole
        # tool exists to avoid.
        truncated = bool(remaining)

    final = probe_once(remaining, python_version, backend=backend,
                        constraints=constraints)
    return {"ok": False, "backend": backend.name, "params": params,
             "failures": failures, "resolved": final.resolved_packages,
             "truncated": truncated}
