"""Post-resolve wheel check: will each resolved package install from a wheel?

A successful `uv pip install --dry-run` says the versions are *resolvable*, not
that they are *installable without a compiler*. A package with no wheel for the
target interpreter is silently built from its sdist: slow, needs a toolchain,
and often fails on a Python the package has not caught up with. Measured on
2026-10-11 (Python 3.15.0rc3, linux x86_64), all of these resolved and were
reported as plain OK:

    PyYAML 6.0.3        wheels cp38..cp314     -> no cp315 wheel
    hdbscan 0.8.44      wheels cp310..cp314    -> no cp315 wheel
    rainbow-api 1.5.3   wheels cp38..cp313     -> no cp315 wheel
    thermosteam         sdist only

so this module asks PyPI for the release's file list and applies the same
tag rules an installer does. It is deliberately dependency-free (no
`packaging`): the tag grammar needed here is small.

Limits, stated rather than hidden:
  * The host platform is matched, not an arbitrary one (no cross-platform
    targeting). Linux: manylinux/musllinux with the host's libc version when it
    can be read (when it cannot, any manylinux of the right arch is accepted);
    Windows: win32/win_amd64/win_arm64; macOS: macosx_* with the host's arch
    and, when readable, macOS version. Other systems accept only `any`.
  * Only PyPI is consulted. Packages resolved from git/file/URL, or from an
    extra index configured in uv, cannot be checked and are listed as such.
  * Free-threaded builds (cp315t) are a different ABI and never match.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_USER_AGENT = "compat-check/0.1 (+https://github.com/jahyunlee00299/compat-check)"
_TIMEOUT = 15
_RETRIES = 1  # one retry on a transient failure, then "unknown"
_MAX_WORKERS = 8

#: How many newest wheel tags to quote for a package that needs a source build.
_NEWEST_SHOWN = 3


class WheelFetchError(Exception):
    """PyPI's file list for a release could not be obtained (reason in str)."""


# --------------------------------------------------------------------------
# target (host) description
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """The interpreter + platform wheels are matched against."""
    python: tuple[int, int]
    system: str                    # "linux" | "windows" | "macos" | other
    machine: str                   # normalized: x86_64, aarch64, arm64, ...
    glibc: tuple[int, int] | None = None
    musl: bool = False
    macos: tuple[int, int] | None = None

    def describe(self) -> str:
        libc = ""
        if self.system == "linux":
            if self.musl:
                libc = " (musl)"
            elif self.glibc:
                libc = f" (glibc {self.glibc[0]}.{self.glibc[1]})"
        return f"Python {self.python[0]}.{self.python[1]} on {self.system} {self.machine}{libc}"


_MACHINE_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64",
                    "aarch64": "aarch64", "i386": "i686", "x86": "i686"}


def _normalize_machine(machine: str, system: str) -> str:
    m = machine.lower()
    m = _MACHINE_ALIASES.get(m, m)
    # Linux calls 64-bit ARM aarch64, macOS and Windows call it arm64.
    if m in ("arm64", "aarch64"):
        return "aarch64" if system == "linux" else "arm64"
    return m


def _parse_pair(text: str) -> tuple[int, int] | None:
    m = re.match(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_python(version: str) -> tuple[int, int]:
    pair = _parse_pair(version)
    if pair is None:
        raise ValueError(f"not a Python version: {version!r}")
    return pair


def host_target(python_version: str) -> Target:
    """Describe this machine as a wheel target for `python_version` ("3.15")."""
    plat = sys.platform
    system = ("linux" if plat.startswith("linux") else
              "windows" if plat in ("win32", "cygwin") else
              "macos" if plat == "darwin" else plat)
    machine = _normalize_machine(platform.machine() or "", system)
    glibc: tuple[int, int] | None = None
    musl = False
    macos: tuple[int, int] | None = None
    if system == "linux":
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION") or ""   # "glibc 2.39"
        except (ValueError, OSError):
            libc = ""
        if libc.startswith("glibc"):
            glibc = _parse_pair(libc.split()[-1])
        elif list(Path("/lib").glob("ld-musl-*")):
            musl = True
    elif system == "macos":
        macos = _parse_pair(platform.mac_ver()[0])
    return Target(python=parse_python(python_version), system=system, machine=machine,
                  glibc=glibc, musl=musl, macos=macos)


# --------------------------------------------------------------------------
# wheel filename / tag matching
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WheelTags:
    python: tuple[str, ...]
    abi: tuple[str, ...]
    platform: tuple[str, ...]

    def label(self) -> str:
        return f"{'.'.join(self.python)}-{'.'.join(self.abi)}-{'.'.join(self.platform)}"


def parse_wheel_filename(filename: str) -> WheelTags | None:
    """Tags of `name-ver[-build]-py-abi-plat.whl`, or None for anything else.

    Compound tags ("py2.py3", "manylinux_2_17_x86_64.manylinux2014_x86_64") are
    split on "."; installers treat them as the cross product, so matching
    reads "any component matches" on each axis.
    """
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    py, abi, plat = parts[-3:]
    return WheelTags(tuple(py.split(".")), tuple(abi.split(".")), tuple(plat.split(".")))


_CP_RE = re.compile(r"^cp3(\d+)$")
_PY_RE = re.compile(r"^py3(\d*)$")


def _python_abi_matches(py_tag: str, abi_tag: str, minor: int) -> bool:
    cp = _CP_RE.match(py_tag)
    if cp:
        wheel_minor = int(cp.group(1))
        if wheel_minor == minor:
            # Exact CPython wheel, its own stable-ABI form, or ABI-free.
            return abi_tag in (f"cp3{minor}", "abi3", "none")
        # abi3 wheels built for an older CPython run on every newer one.
        return wheel_minor < minor and abi_tag == "abi3"
    py = _PY_RE.match(py_tag)
    if py:
        # "py3" = any Python 3; "py310" = Python >= 3.10 pure-python flag.
        return abi_tag == "none" and (py.group(1) == "" or int(py.group(1)) <= minor)
    return False


_MANYLINUX_LEGACY = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
_MANYLINUX_RE = re.compile(r"^manylinux_(\d+)_(\d+)_(.+)$")
_MUSLLINUX_RE = re.compile(r"^musllinux_\d+_\d+_(.+)$")
_MACOS_RE = re.compile(r"^macosx_(\d+)_(\d+)_(.+)$")


def _platform_matches(tag: str, target: Target) -> bool:
    if tag == "any":
        return True
    if target.system == "linux":
        if tag == f"linux_{target.machine}":
            return True
        m = _MUSLLINUX_RE.match(tag)
        if m:
            return target.musl and m.group(1) == target.machine
        required = next((v for k, v in _MANYLINUX_LEGACY.items()
                         if tag == f"{k}_{target.machine}"), None)
        if required is None:
            m = _MANYLINUX_RE.match(tag)
            if m and m.group(3) == target.machine:
                required = (int(m.group(1)), int(m.group(2)))
        if required is None or target.musl:
            return False
        # Unknown host glibc: accept (documented limit) rather than guess "no".
        return target.glibc is None or required <= target.glibc
    if target.system == "windows":
        return tag == {"x86_64": "win_amd64", "arm64": "win_arm64",
                       "i686": "win32"}.get(target.machine)
    if target.system == "macos":
        m = _MACOS_RE.match(tag)
        if not m:
            return False
        arch = m.group(3)
        arch_ok = (arch == target.machine or arch == "universal2"
                   or (arch == "intel" and target.machine in ("x86_64", "i686")))
        needed = (int(m.group(1)), int(m.group(2)))
        return arch_ok and (target.macos is None or needed <= target.macos)
    return False


def wheel_matches_platform(tags: WheelTags, target: Target) -> bool:
    return any(_platform_matches(p, target) for p in tags.platform)


def wheel_is_compatible(filename: str, target: Target) -> bool:
    """True when an installer on `target` would accept this wheel file."""
    tags = parse_wheel_filename(filename)
    if tags is None or not wheel_matches_platform(tags, target):
        return False
    minor = target.python[1]
    if target.python[0] != 3:
        return False
    return any(_python_abi_matches(py, abi, minor)
               for py in tags.python for abi in tags.abi)


# --------------------------------------------------------------------------
# PyPI access
# --------------------------------------------------------------------------

def fetch_release_files(name: str, version: str) -> list[dict]:
    """The `urls` array of https://pypi.org/pypi/<name>/<version>/json.

    Raises WheelFetchError with a human-readable reason for every failure: a
    wheel check that cannot reach PyPI must say "unknown", never look clean.
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    last = "unknown error"
    for _ in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return list(data.get("urls") or [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Not retried: the release is not on PyPI (private index, local
                # build, or a typo). Say so; it is not "no wheels".
                raise WheelFetchError(
                    f"{name}=={version} is not on PyPI (HTTP 404); resolved from "
                    f"another index?") from e
            last = f"HTTP {e.code} from PyPI"
        except urllib.error.URLError as e:
            last = f"network error: {e.reason}"
        except (TimeoutError, OSError) as e:
            last = f"network error: {e}"
        except ValueError as e:  # json.JSONDecodeError
            last = f"unreadable PyPI response: {e}"
    raise WheelFetchError(last)


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

@dataclass
class WheelFinding:
    spec: str                      # as resolved: "PyYAML==6.0.3" or "pkg @ git+..."
    status: str                    # "wheel" | "source" | "unknown" | "non-pypi"
    detail: str = ""               # source: newest wheels; unknown: reason; non-pypi: source
    wheel: str | None = None       # the compatible wheel file, when status == "wheel"


@dataclass
class WheelReport:
    target: Target
    findings: list[WheelFinding] = field(default_factory=list)

    def _by(self, status: str) -> list[WheelFinding]:
        return [f for f in self.findings if f.status == status]

    @property
    def source_builds(self) -> list[WheelFinding]:
        return self._by("source")

    @property
    def unknown(self) -> list[WheelFinding]:
        return self._by("unknown")

    @property
    def non_pypi(self) -> list[WheelFinding]:
        return self._by("non-pypi")

    @property
    def with_wheels(self) -> list[WheelFinding]:
        return self._by("wheel")

    @property
    def needs_attention(self) -> bool:
        """A source build or an unanswerable package. Non-PyPI entries are
        reported but are not a defect — they could not be asked."""
        return bool(self.source_builds or self.unknown)

    def flag_for(self, spec: str) -> str:
        for f in self.findings:
            if f.spec == spec:
                return {"source": "source build", "unknown": "wheel status unknown",
                        "non-pypi": "non-PyPI source"}.get(f.status, "")
        return ""


_PINNED_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>\S+)$")
_DIRECT_URL_RE = re.compile(r"^(?P<name>\S+)\s+@\s+(?P<url>\S+)")


def _newest_wheel_tags(files: list[dict], target: Target) -> str:
    """Why nothing fits: the newest python/abi tags shipped for THIS platform."""
    wheels = [parse_wheel_filename(f.get("filename", ""))
              for f in files if f.get("packagetype") == "bdist_wheel"]
    wheels = [w for w in wheels if w is not None]
    if not wheels:
        return "sdist only (no wheels published)"
    here = [w for w in wheels if wheel_matches_platform(w, target)]
    if not here:
        plats = sorted({p for w in wheels for p in w.platform})
        shown = ", ".join(plats[:4]) + (", ..." if len(plats) > 4 else "")
        return f"wheels exist only for other platforms ({shown})"

    def minor(w: WheelTags) -> int:
        digits = [int(m.group(1)) for t in w.python if (m := re.match(r"^(?:cp|py)3(\d+)", t))]
        return max(digits, default=-1)

    pairs: list[str] = []
    for w in sorted(here, key=minor, reverse=True):
        pair = f"{w.python[0]}-{w.abi[0]}"
        if pair not in pairs:
            pairs.append(pair)
    return "newest wheels: " + ", ".join(pairs[:_NEWEST_SHOWN])


def _check_one(spec: str, target: Target,
                fetch: Callable[[str, str], list[dict]]) -> WheelFinding:
    pinned = _PINNED_RE.match(spec)
    if pinned is None:
        direct = _DIRECT_URL_RE.match(spec)
        if direct:
            return WheelFinding(spec, "non-pypi", detail=direct.group("url"))
        return WheelFinding(spec, "unknown", detail="unrecognized resolver output")
    name, version = pinned.group("name"), pinned.group("version")
    try:
        files = fetch(name, version)
    except WheelFetchError as e:
        return WheelFinding(spec, "unknown", detail=str(e))
    live = [f for f in files if not f.get("yanked")]
    for f in live:
        if f.get("packagetype") == "bdist_wheel" and wheel_is_compatible(
                f.get("filename", ""), target):
            return WheelFinding(spec, "wheel", wheel=f.get("filename"))
    return WheelFinding(spec, "source", detail=_newest_wheel_tags(live, target))


def check_wheels(resolved: list[str], target: Target,
                  fetch: Callable[[str, str], list[dict]] | None = None,
                  max_workers: int = _MAX_WORKERS) -> WheelReport:
    """Classify every resolved package: wheel / source build / unknown / non-PyPI.

    `fetch` is injectable so tests never touch the network. Input order is
    preserved in the report.
    """
    fetch = fetch or fetch_release_files
    if not resolved:
        return WheelReport(target)
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(resolved)))) as pool:
        findings = list(pool.map(lambda s: _check_one(s, target, fetch), resolved))
    return WheelReport(target, findings)


def format_report(report: WheelReport) -> list[str]:
    """Report lines for the CLI. The WARN sections are what a reader must not miss."""
    t = report.target.describe()
    lines: list[str] = []
    checked = len(report.with_wheels) + len(report.source_builds) + len(report.unknown)
    if not report.needs_attention:
        if checked:
            lines.append(f"wheels: all {checked} PyPI package(s) have a wheel for {t}")
    if report.source_builds:
        lines.append(
            f"WARN — {len(report.source_builds)} package(s) have no wheel for "
            f"{t}; install will build from source:")
        for f in report.source_builds:
            lines.append(f"  ! {f.spec}   {f.detail}")
    if report.unknown:
        lines.append(
            f"WARN — wheel status UNKNOWN for {len(report.unknown)} package(s) "
            f"(not assumed fine):")
        for f in report.unknown:
            lines.append(f"  ? {f.spec}   {f.detail}")
    if report.non_pypi:
        lines.append(
            f"note: {len(report.non_pypi)} package(s) come from a non-PyPI source "
            f"and were not wheel-checked:")
        for f in report.non_pypi:
            lines.append(f"  ~ {f.spec}")
    return lines
