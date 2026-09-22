"""Parse the lines of a requirements.txt the way pip actually parses them.

Why this module is separate from fetcher.py
-------------------------------------------
fetcher.py used to filter lines with
``line.startswith(("-r ", "-e ", "--"))`` and treat everything else as a
requirement. Benchmarking against pip's own ``req_file.py`` showed that prefix
matching produces three distinct wrong answers, all of which reached the
resolver and were reported to the user as a failure of *their* repo:

* a line ending in ``\\`` was never joined to the next, so ``requests \\`` and
  ``>=2.0`` became two malformed specs instead of one requirement
* a trailing ``# comment`` was never stripped, so uv received
  ``requests>=2.0  # pinned`` and answered ``Failed to parse``
* short options other than ``-r``/``-e`` (``-c``, ``-i``, ``-f``) did not match
  the prefix filter and were handed to the resolver as package names

pip avoids the whole class by never pattern-matching prefixes: it parses each
line against an explicit table of known options. This module does the same,
with a table rather than optparse, because we only need to *classify* a line —
we never act on the option's value the way pip does.

Environment variables
---------------------
pip expands ``${VAR}`` in requirement lines from its own environment. We
deliberately do NOT: this tool reads strangers' repositories, and expanding
``--index-url https://${TOKEN}@pypi.internal/simple`` from our environment
would splice a local secret into a subprocess argument. Such lines are
recorded as skipped instead, because a private index also means our answer is
incomplete by construction — we cannot see what lives there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A comment runs to end of line. pip requires a comment to be preceded by
# whitespace or start the line, so "pkg#egg=x" is not a comment.
_COMMENT_RE = re.compile(r"(^|\s+)#.*$")

# pip only recognises the ${NAME} form (pypa/pip#3514), deliberately, so a
# bare "$" in a requirement is not mistaken for a variable.
_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

#: Options that take a path/URL argument we care about structurally.
INCLUDE_OPTIONS = ("-r", "--requirement")
CONSTRAINT_OPTIONS = ("-c", "--constraint")
EDITABLE_OPTIONS = ("-e", "--editable")

#: Options that change resolution but whose value we do not follow. Listed
#: explicitly — the bug this module fixes came from guessing by prefix.
_VALUE_OPTIONS = (
    "-i", "--index-url",
    "--extra-index-url",
    "-f", "--find-links",
    "--no-binary", "--only-binary",
    "--trusted-host",
    "--use-feature",
    "--use-deprecated",
    "--config-settings",
)
_FLAG_OPTIONS = (
    "--pre",
    "--prefer-binary",
    "--require-hashes",
    "--no-index",
)


@dataclass
class ParsedRequirements:
    """The outcome of parsing ONE requirements file (includes not yet followed).

    ``includes`` and ``editables`` are what the caller must resolve to make the
    result complete; until it does, ``requirements`` is only a partial answer
    and must never be handed to a resolver on its own.
    """
    requirements: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)      # -r / --requirement
    constraints: list[str] = field(default_factory=list)   # -c / --constraint
    editables: list[str] = field(default_factory=list)     # -e / --editable
    skipped: list[str] = field(default_factory=list)       # recognised, not followed

    @property
    def is_complete(self) -> bool:
        """True when nothing was left unresolved by this file alone."""
        return not (self.includes or self.constraints or self.editables)


def join_continuations(text: str) -> list[str]:
    """Join lines ending in a backslash, as pip's join_lines() does.

    A comment line is never joined to the next, even if it ends in a
    backslash — otherwise a commented-out continuation would swallow the
    following requirement.
    """
    joined: list[str] = []
    buffer: list[str] = []

    for raw in text.splitlines():
        stripped = raw.strip()
        is_comment = stripped.startswith("#")

        if not is_comment and raw.rstrip().endswith("\\"):
            buffer.append(raw.rstrip()[:-1])
            continue

        if buffer:
            buffer.append(raw)
            joined.append("".join(buffer))
            buffer = []
        else:
            joined.append(raw)

    if buffer:  # file ended on a continuation
        joined.append("".join(buffer))
    return joined


def strip_comment(line: str) -> str:
    """Remove a trailing comment. ``pkg#egg=name`` keeps its fragment."""
    return _COMMENT_RE.sub("", line)


def env_vars_in(line: str) -> list[str]:
    """Names of ``${VAR}`` references in a line. Never expanded — see module doc."""
    return _ENV_VAR_RE.findall(line)


def _split_option(line: str) -> tuple[str, str]:
    """Split ``-r foo.txt`` / ``--requirement=foo.txt`` into (option, value)."""
    if "=" in line and line.split("=", 1)[0].startswith("--"):
        opt, value = line.split("=", 1)
        return opt.strip(), value.strip()
    parts = line.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def parse_lines(text: str) -> ParsedRequirements:
    """Classify every line of one requirements file.

    Does not follow includes — that needs network access and a cycle guard,
    which live in fetcher.py. This function stays pure and testable with a
    plain string.
    """
    result = ParsedRequirements()

    for line in join_continuations(text):
        line = strip_comment(line).strip()
        if not line:
            continue

        referenced = env_vars_in(line)
        if referenced:
            # Never expand: the value would come from OUR environment while we
            # are reading someone else's repository.
            result.skipped.append(
                f"{line}  (references environment variable(s): "
                f"{', '.join('${' + v + '}' for v in referenced)}; not expanded)"
            )
            continue

        if not line.startswith("-"):
            result.requirements.append(line)
            continue

        option, value = _split_option(line)

        if option in INCLUDE_OPTIONS:
            if not value:
                raise ValueError(f"{option} given without a file: {line!r}")
            result.includes.append(value)
        elif option in CONSTRAINT_OPTIONS:
            if not value:
                raise ValueError(f"{option} given without a file: {line!r}")
            result.constraints.append(value)
        elif option in EDITABLE_OPTIONS:
            if not value:
                raise ValueError(f"{option} given without a target: {line!r}")
            result.editables.append(value)
        elif option in _VALUE_OPTIONS or option in _FLAG_OPTIONS:
            result.skipped.append(line)
        else:
            # An option we do not know. Recording it as skipped is safer than
            # the old behaviour (pass it through as a requirement), which is
            # exactly how "-i https://x" reached the resolver as a package.
            result.skipped.append(f"{line}  (unrecognised option)")

    return result
