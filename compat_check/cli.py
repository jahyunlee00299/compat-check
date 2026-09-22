"""compat-check CLI entry point — wires fetcher -> cache -> runner into a
human-readable report.

    compat-check <github-url-or-pypi-name> [--python 3.11] [--no-cache]
"""
from __future__ import annotations

import argparse
import sys

from compat_check.cache import cached_probe_all
from compat_check.fetcher import FetchError, fetch_requirements_detailed
from compat_check.params import ParamError, validate_python_version
from compat_check.render import render_tree
from compat_check.runner import probe_all
from compat_check.tree import build_tree


def _print_report(source: str, requirements: list[str], result: dict,
                   fetched=None) -> None:
    cache_note = " (cached)" if result.get("cache_hit") else ""
    print(f"compat-check: {source}{cache_note}")
    print(f"backend: {result['backend']}")

    # A parameter the backend could not honour is stated here rather than
    # silently applied — the report must describe the probe that actually ran.
    params = result.get("params")
    if params is not None:
        if params.python_was_ignored:
            print(f"python: {params.effective_python} "
                  f"(requested {params.requested_python} — NOT honoured)")
        else:
            print(f"python: {params.effective_python}")
        for warning in params.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
    if fetched is not None and fetched.source_file:
        print(f"source file: {fetched.source_file}")
    print(f"requirements checked: {', '.join(requirements)}")
    if fetched is not None and fetched.constraints:
        # Constraints bound the resolution without being installed. Saying so
        # explains why a version differs from what a bare probe would pick.
        print(f"constraints applied: {len(fetched.constraints)} "
              f"(from -c files; they pin versions without requesting install)")
    print()
    if fetched is not None:
        for note in fetched.skipped:
            print(f"  note: {note}", file=sys.stderr)

    if result["ok"]:
        print(f"OK — {len(result['resolved'])} package(s) would install cleanly:")
        for pkg in result["resolved"]:
            print(f"  + {pkg}")
        return

    if result.get("truncated"):
        print(f"PROBLEMS FOUND — at least {len(result['failures'])} package(s) cannot "
              f"be resolved (the probe hit its round limit; there may be more):")
    else:
        print(f"PROBLEMS FOUND — {len(result['failures'])} package(s) cannot be resolved:")
    for f in result["failures"]:
        print(f"\n  [{f['package']}]")
        # Indent the raw resolver output so it reads as evidence, not noise.
        for line in f["stderr"].strip().splitlines():
            print(f"    {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compat-check",
        description="Check whether a GitHub repo or PyPI package would install "
                    "cleanly in this environment, without actually installing it.",
    )
    parser.add_argument("source", help="GitHub repo URL or PyPI package name")
    parser.add_argument("--python", default="3.11", dest="python_version",
                         help="target Python version (default: 3.11). The pip fallback "
                              "backend cannot honour this and will say so explicitly.")
    parser.add_argument("--no-cache", action="store_true",
                         help="skip the local failure-history cache, always probe fresh")
    parser.add_argument("--tree", action="store_true",
                         help="show the full dependency tree (requires uv; no pip fallback)")
    args = parser.parse_args(argv)

    try:
        # Validate before the network call: a typo in --python should not cost
        # a GitHub round-trip before it is reported.
        validate_python_version(args.python_version)
    except ParamError as e:
        print(f"compat-check: {e}", file=sys.stderr)
        return 3

    try:
        fetched = fetch_requirements_detailed(args.source)
    except FetchError as e:
        print(f"compat-check: could not resolve requirements for '{args.source}': {e}", file=sys.stderr)
        return 2

    requirements = fetched.requirements
    constraints = fetched.constraints

    if args.no_cache:
        result = probe_all(requirements, python_version=args.python_version,
                            constraints=constraints)
        result = {**result, "cache_hit": False}
    else:
        result = cached_probe_all(requirements, python_version=args.python_version,
                                   constraints=constraints)

    _print_report(args.source, requirements, result, fetched)

    if args.tree:
        print()
        # Same constraints as the probe above, or the tree would describe a
        # different resolution than the report the user just read.
        tree_result = build_tree(requirements, python_version=args.python_version,
                                  constraints=constraints)
        if tree_result["ok"]:
            print(render_tree(tree_result["roots"], label=args.source))
        else:
            print(f"(tree unavailable: {tree_result['stderr'].strip()})", file=sys.stderr)

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
