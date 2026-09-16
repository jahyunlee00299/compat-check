"""Run `uv pip install --dry-run` against a disposable venv and collect failures.

uv's resolver is fail-fast: a single dry-run call reports only the first
unsatisfiable requirement. To surface every problem in a requirements list,
this module drops each failing package and retries until the resolve
succeeds or stops making progress.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_NO_VERSION_RE = re.compile(r"no version of ([A-Za-z0-9_.\-]+)")
_UNSATISFIABLE_RE = re.compile(
    r"because you require ([A-Za-z0-9_.\-]+)[><=! ].*and ([A-Za-z0-9_.\-]+)[><=! ]",
    re.IGNORECASE,
)
_ABI_MISMATCH_RE = re.compile(r"([A-Za-z0-9_.\-]+) \(v[^)]+\) has no wheels")


@dataclass
class ProbeResult:
    ok: bool
    stdout: str
    stderr: str
    resolved_packages: list[str] = field(default_factory=list)
    failing_package: str | None = None


def _extract_failing_package(stderr: str) -> str | None:
    m = _ABI_MISMATCH_RE.search(stderr)
    if m:
        return m.group(1)
    m = _NO_VERSION_RE.search(stderr)
    if m:
        return m.group(1)
    m = _UNSATISFIABLE_RE.search(stderr)
    if m:
        return m.group(1)
    return None


def probe_once(requirements: list[str], python_version: str = "3.11") -> ProbeResult:
    """Create a throwaway venv and dry-run install the given requirement specs."""
    with tempfile.TemporaryDirectory(prefix="compat_check_") as tmp:
        venv_path = Path(tmp) / "venv"
        create = subprocess.run(
            ["uv", "venv", str(venv_path), "--python", python_version],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if create.returncode != 0:
            return ProbeResult(ok=False, stdout=create.stdout, stderr=create.stderr)

        if not requirements:
            return ProbeResult(ok=True, stdout="", stderr="")

        install = subprocess.run(
            ["uv", "pip", "install", "--dry-run", "--python", str(venv_path), *requirements],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        ok = install.returncode == 0
        resolved = []
        if ok:
            # uv writes the install plan to stderr, not stdout.
            resolved = [
                line.strip(" +")
                for line in install.stderr.splitlines()
                if line.strip().startswith("+")
            ]
        failing = None if ok else _extract_failing_package(install.stderr)
        return ProbeResult(
            ok=ok, stdout=install.stdout, stderr=install.stderr,
            resolved_packages=resolved, failing_package=failing,
        )


def probe_all(requirements: list[str], python_version: str = "3.11", max_rounds: int = 20) -> dict:
    """Repeatedly probe, dropping each failing package, to surface every failure.

    Returns {"ok": bool, "failures": [{"package": str, "stderr": str}], "resolved": [str]}.
    """
    remaining = list(requirements)
    failures: list[dict] = []
    seen_failing: set[str] = set()

    for _ in range(max_rounds):
        result = probe_once(remaining, python_version)
        if result.ok:
            return {"ok": not failures, "failures": failures, "resolved": result.resolved_packages}

        pkg = result.failing_package
        if pkg is None or pkg in seen_failing:
            # Can't attribute the failure to a single package, or stuck in a loop.
            failures.append({"package": pkg or "<unknown>", "stderr": result.stderr})
            break

        seen_failing.add(pkg)
        failures.append({"package": pkg, "stderr": result.stderr})
        remaining = [r for r in remaining if not r.lower().startswith(pkg.lower())]

    final = probe_once(remaining, python_version)
    return {"ok": False, "failures": failures, "resolved": final.resolved_packages}
