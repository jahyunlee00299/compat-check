"""Follow ``-r`` / ``-c`` / ``-e .`` references so a requirement list is complete.

The contract this module exists to hold
---------------------------------------
``fetch_requirements()`` must return the COMPLETE requirement list or raise.
Before this module, a ``requirements.txt`` containing ``-r base.txt`` silently
dropped everything in ``base.txt`` and the caller was handed the remainder as
if it were the whole set — a confident wrong answer, the same failure class as
the ``--python`` bug fixed in 0.2.0.

Cycle detection follows pip's ``_parse_and_recurse``: a dict per branch,
``{path: file_that_first_included_it}``, copied on each descent — NOT a shared
``seen`` set. The difference is load-bearing here because an unresolvable
include is fatal. With a shared set, a legal diamond

    dev.txt  -> base.txt
    dev.txt  -> test.txt -> base.txt

is indistinguishable from a true cycle: both present as "already visited". The
per-branch dict marks ``base.txt`` only along the path that reached it, so the
diamond resolves twice (deduplicated afterwards) while ``a -> b -> a`` raises
and can name where ``a`` was first included.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Protocol

from compat_check.reqline import ParsedRequirements, parse_lines

#: Bounds so one CLI call cannot become hundreds of HTTP requests.
MAX_DEPTH = 8
MAX_FILES = 32


class IncludeError(Exception):
    """An include could not be resolved, so the requirement set is unknowable."""


class Resolver(Protocol):
    """Fetches a requirements file by repo-relative path, or returns None (404)."""

    def __call__(self, path: str) -> str | None: ...


@dataclass
class ResolvedRequirements:
    requirements: list[str] = field(default_factory=list)
    #: Entries from ``-c`` files. A constraint means "IF something pulls this
    #: package in, pin it here" — it is not an instruction to install. Kept
    #: separate from ``requirements`` because merging the two turns every pin
    #: into a requirement: measured on home-assistant/core, that inflated 51
    #: real requirements to 181, of which 130 existed only as constraints.
    constraints: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: ``-e`` targets that name a directory in this repo, for the caller to
    #: resolve through the normal candidate-file search.
    local_editables: list[str] = field(default_factory=list)


def _normalize(base_file: str, target: str) -> str:
    """Resolve ``target`` relative to the directory holding ``base_file``.

    Repo paths are POSIX regardless of host OS — these are URL paths on
    raw.githubusercontent.com, never local filesystem paths, so ntpath
    semantics must not leak in.
    """
    base_dir = posixpath.dirname(base_file)
    return posixpath.normpath(posixpath.join(base_dir, target)) if base_dir else posixpath.normpath(target)


def _is_local_editable(target: str) -> bool:
    """True for ``-e .`` / ``-e ./pkg`` — this repo. False for ``-e git+https://...``."""
    return not (
        "://" in target
        or target.startswith(("git+", "hg+", "svn+", "bzr+"))
    )


def resolve_includes(
    root_file: str,
    text: str,
    resolver: Resolver,
    *,
    max_depth: int = MAX_DEPTH,
    max_files: int = MAX_FILES,
) -> ResolvedRequirements:
    """Parse ``text`` (the contents of ``root_file``) and follow every include.

    Raises IncludeError when an include cannot be read, a cycle exists, or a
    bound is exceeded — never returns a partial list.
    """
    out = ResolvedRequirements()
    budget = {"files": 0}

    def descend(
        path: str,
        content: str,
        depth: int,
        chain: dict[str, str | None],
        *,
        as_constraint: bool = False,
    ) -> None:
        if depth > max_depth:
            raise IncludeError(
                f"include depth exceeded {max_depth} following {path!r} "
                f"(chain: {' -> '.join(chain)})"
            )
        budget["files"] += 1
        if budget["files"] > max_files:
            raise IncludeError(
                f"followed more than {max_files} requirement files; "
                f"refusing to continue at {path!r}"
            )

        out.files_read.append(path)
        try:
            parsed: ParsedRequirements = parse_lines(content)
        except ValueError as e:
            raise IncludeError(f"{path}: {e}") from e

        target_list = out.constraints if as_constraint else out.requirements
        target_list.extend(parsed.requirements)
        out.skipped.extend(f"{path}: {s}" for s in parsed.skipped)

        for target in parsed.editables:
            if _is_local_editable(target):
                out.local_editables.append(_normalize(path, target))
            else:
                # A different project entirely. Following it would probe the
                # wrong repo; ignoring it silently would under-report. Say so.
                out.skipped.append(
                    f"{path}: -e {target}  (external VCS editable; not followed)"
                )

        # -r includes contribute requirements; -c constraints contribute pins.
        # Both are followed (a constraint file can itself use -c), but their
        # contents land in different lists — see ResolvedRequirements.constraints.
        for target, is_constraint in (
            [(t, False) for t in parsed.includes]
            + [(t, True) for t in parsed.constraints]
        ):
            child = _normalize(path, target)
            if child in chain:
                first = chain[child]
                where = (
                    f", first included by {first}" if first
                    else " (it is the file the traversal started from)"
                )
                raise IncludeError(
                    f"{child} is included recursively from {path}{where}"
                )
            child_text = resolver(child)
            if child_text is None:
                raise IncludeError(
                    f"{path} includes {target!r} but {child} could not be read; "
                    f"the full requirement set is unknown"
                )
            descend(child, child_text, depth + 1, {**chain, child: path},
                     as_constraint=as_constraint or is_constraint)

    descend(root_file, text, 0, {root_file: None})

    # A file reached by two legal paths contributes its entries twice.
    def dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out_ = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out_.append(item)
        return out_

    out.requirements = dedupe(out.requirements)
    out.constraints = dedupe(out.constraints)
    return out
