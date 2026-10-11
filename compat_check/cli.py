"""compat-check CLI entry point — wires fetcher -> cache -> runner into a
human-readable report.

    compat-check <github-url-or-pypi-name> [--python 3.11] [--no-cache]
                 [--strict-wheels] [--no-wheel-check]

Exit codes: 0 clean (or resolves with wheel WARNINGs, see --strict-wheels),
1 conflicts found, 2 source could not be resolved (BLIND), 3 invalid parameter.
"""
from __future__ import annotations

import argparse
import sys

from compat_check.cache import cached_probe_all
from compat_check.fetcher import FetchError, fetch_requirements_detailed
from compat_check.params import ParamError, validate_python_version
from compat_check.render import render_tree
from compat_check.runner import normalize_name, probe_all
from compat_check.tree import build_tree
from compat_check.wheels import WheelReport, check_wheels, format_report, host_target


def _describe_blocker(failure: dict) -> str:
    """" (blocked by onnxruntime: no cp315 wheel)" when uv names a root cause.

    The dropped requirement and the package that really blocks it differ for
    every transitive failure (markitdown -> magika -> onnxruntime).
    """
    root, reason = failure.get("root_package"), failure.get("root_reason")
    if not root:
        return ""
    if normalize_name(root) == normalize_name(failure["package"]):
        return f" ({reason})"
    return f" (blocked by {root}: {reason})"


def _print_report(source: str, requirements: list[str], result: dict,
                   fetched=None, wheels: WheelReport | None = None,
                   strict_wheels: bool = False) -> None:
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
        if wheels is not None and wheels.needs_attention:
            # Resolvable is not the same as installable from wheels: say so in
            # the headline, not only in a section below the package list.
            print(f"RESOLVES WITH WARNINGS — {len(result['resolved'])} package(s) "
                  f"resolve, but {len(wheels.source_builds)} need a source build"
                  + (f" and {len(wheels.unknown)} have unknown wheel status"
                     if wheels.unknown else "") + ":")
        else:
            print(f"OK — {len(result['resolved'])} package(s) would install cleanly:")
        for pkg in result["resolved"]:
            flag = wheels.flag_for(pkg) if wheels is not None else ""
            print(f"  + {pkg}" + (f"   [{flag}]" if flag else ""))
        if wheels is not None:
            lines = format_report(wheels)
            if lines:
                print()
                for line in lines:
                    print(line)
            if wheels.needs_attention:
                if strict_wheels:
                    print("\nresult: FAIL (--strict-wheels) — resolves, but not everything "
                          "installs from wheels (exit 1)")
                else:
                    print("\nresult: WARN — resolves, but not everything installs from wheels "
                          "(exit 0; --strict-wheels makes this exit 1)")
        return

    if result.get("truncated"):
        print(f"PROBLEMS FOUND — at least {len(result['failures'])} package(s) cannot "
              f"be resolved (the probe hit its round limit; there may be more):")
    else:
        print(f"PROBLEMS FOUND — {len(result['failures'])} package(s) cannot be resolved:")
    for f in result["failures"]:
        print(f"\n  [{f['package']}]{_describe_blocker(f)}")
        tags = f.get("available_tags")
        if tags:
            # uv's own closing hint, distilled: which interpreters the blocking
            # package does ship wheels for. Raw wording stays in the evidence.
            print(f"    wheels for {f.get('tags_for') or 'it'} exist only for: "
                  f"{', '.join(tags)}")
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
    parser.add_argument("--strict-wheels", action="store_true",
                         help="exit 1 when the resolve succeeds but a package would be built "
                              "from source (no wheel for the target Python on this platform) "
                              "or its wheel status could not be determined")
    parser.add_argument("--no-wheel-check", action="store_true",
                         help="skip the post-resolve wheel check (it makes one PyPI request "
                              "per resolved package)")
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

    wheels = None
    if result["ok"] and result["resolved"] and not args.no_wheel_check:
        params = result.get("params")
        python = params.effective_python if params is not None else args.python_version
        wheels = check_wheels(result["resolved"], host_target(python))

    _print_report(args.source, requirements, result, fetched, wheels,
                  strict_wheels=args.strict_wheels)

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

    if not result["ok"]:
        return 1
    # Exit-code contract shared with the other gates is 0 / 1 / 2 (2 = BLIND):
    # "resolves but needs a source build" gets no code of its own. It stays 0
    # with a prominent WARN, and --strict-wheels promotes it to 1.
    if args.strict_wheels and wheels is not None and wheels.needs_attention:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
