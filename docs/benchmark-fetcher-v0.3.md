# Benchmark — fetcher v0.3 design vs. the reference implementations

The v0.3 design was written from our own code plus live probing. Before
implementing it, it was checked against the two projects that have already
solved these exact problems:

- **pip** — `pip/_internal/req/req_file.py` (622 lines), the canonical
  requirements.txt parser. Ground truth for what a requirements file *is*.
- **setuptools** — `setuptools/config/expand.py` (`StaticModule`,
  `read_attr`), which reads `install_requires` from a module **without
  executing it**. Ground truth for the AST approach in Unit 2.

Both were read as source, and every claim below was executed, not inferred.

## Verdict

The design's *structure* held up: the parser/network split, the include
recursion, the "raise rather than return a partial list" contract, and the
refusal to execute `setup.py` all match what the reference implementations do
(or, in one case, are deliberately stricter — see §5).

What the benchmark found is that the design was **scoped too narrowly**. It
treated `-r` and `-e` as the whole problem. pip's parser shows the requirement
*line* itself needs work that the design never mentioned — and two of those
gaps are live defects in the shipped 0.2.0, not future work.

## 1. Line continuation — MISSING from the design, live defect

pip's `join_lines()` joins a line ending in `\` with the next one. Our parser
does not:

```
requests \
    >=2.0
flask
```

| | result |
|---|---|
| pip | one requirement, `requests >=2.0` |
| compat-check 0.2.0 | `['requests \\', '>=2.0', 'flask']` |

Two malformed specs where there should be one. Confirmed to reach the resolver.

## 2. Trailing comments — MISSING from the design, live defect

pip strips a trailing `# ...` from every line before parsing. We only skip
lines that *start* with `#`:

```
requests>=2.0  # pinned for security
```

parses to the literal string `'requests>=2.0  # pinned for security'`, which
uv rejects with:

```
error: Failed to parse: `requests>=2.0  # pinned`
```

reported to the user as a failure of package `<unknown>`. The user is told
their repo has a dependency problem; the problem is our parser.

## 3. Short option flags leak — MISSING from the design, live defect

Our filter is `line.startswith(("-r ", "-e ", "--"))`. That covers long
options and two short ones. Everything else short passes through as a
"requirement":

| line | pip | compat-check 0.2.0 |
|---|---|---|
| `-c constraints.txt` | constraint file | kept as a requirement |
| `-i https://x` | index URL | kept as a requirement |
| `-f https://z` | find-links | kept as a requirement |

Verified end-to-end: `probe_all(["-i https://x"])` returns a failure whose
stderr is `error: the following required arguments were not provided` — uv
parsing our garbage, attributed to package `<unknown>`.

pip does not pattern-match prefixes at all: it runs each line through an
`optparse` parser built from an explicit `SUPPORTED_OPTIONS` list. **Adopt the
explicit-option-table approach**; prefix matching is what produced all three
of these defects.

## 4. Environment variables — MISSING from the design, security-relevant

pip expands `${VAR}` in requirement lines via `os.getenv` before parsing
(`expand_env_variables`, restricted to `${NAME}` form deliberately, per
pypa/pip#3514).

Implication the design never considered: **a requirements.txt can reference a
credential** (`--index-url https://${TOKEN}@pypi.internal/simple`). Two
consequences for us:

- We must not expand these from *our own* environment when probing a stranger's
  repo — that would splice a local secret into a subprocess argument.
- Any such line must be reported as "skipped, references an environment
  variable", because a private index means our answer is incomplete by
  construction: we cannot see those packages.

The design's blanket "`--index-url` is skipped" was right by accident, for the
wrong reason, and said nothing about the secret.

## 5. Cycle detection — design was weaker than pip's

Design: a shared `seen: set[str]`.
pip: a **dict per branch**, `{abspath: first_including_file}`, copied on each
descent (`new_parsed_files = parsed_files.copy()`), raising only on a true
self-reference and naming where the file was first included.

The difference shows on a diamond:

```
dev.txt  -> base.txt
dev.txt  -> test.txt -> base.txt
```

A shared set marks `base.txt` seen on the first path and silently skips it on
the second. That happens to be harmless for deduplication, but it destroys the
distinction the design depends on: with a set, "already included via another
path" and "cycle" are the same state — and the design declares an unresolvable
include **fatal**. We would raise on a legal diamond, or skip a real cycle.

**Adopt pip's per-branch dict.** It is the same cost and it carries the
provenance the `FetchResult` needs anyway.

## 6. `setup.py` via AST — design confirmed, with one deliberate divergence

setuptools' `StaticModule` does exactly what Unit 2 proposed: `ast.parse`, walk
top-level `Assign`/`AnnAssign`, `ast.literal_eval` the value. Our plan to
resolve `REQUIRES = [...]; setup(install_requires=REQUIRES)` is the same shape
as `_find_assignments()`. Confirmed.

**The divergence is the fallback, and it must not be copied.** `read_attr()`
ends with:

```python
except Exception:  # intentional broad fallback
    module = _load_spec(spec, module_name)   # <- imports and EXECUTES it
    return getattr(module, attr_name)
```

setuptools may do this: it is building *your own* project, which you already
trust. compat-check reads **arbitrary third-party repos over the network**. The
design's "raise a FetchError saying requirements are computed dynamically" is
the correct behaviour here and is *stricter* than the reference implementation
on purpose. Record it as such, so nobody later "fixes" it to match setuptools.

## Changes to the design

New unit, sequenced first — it is where the live defects are:

**Unit 0 — requirement-line parsing (correctness, ships before everything
else)**

- `join_lines()` equivalent for `\` continuations
- strip trailing comments (`COMMENT_RE`), not just whole-line comments
- replace prefix matching with an explicit option table: `-r/--requirement`,
  `-c/--constraint`, `-e/--editable`, `-i/--index-url`, `--extra-index-url`,
  `-f/--find-links`, `--no-binary`, `--only-binary`, `--pre`,
  `--prefer-binary`, `--require-hashes`, `--trusted-host`, `--use-feature`
- detect `${VAR}` references, never expand them, report them as skipped

Revised elsewhere:

- **Unit 1**: cycle guard becomes pip's per-branch dict, not a shared set
- **Unit 2**: unchanged, plus an explicit note that the no-execute rule is a
  deliberate divergence from setuptools' fallback
- **Unit 3**: unchanged

Unit 0's three defects exist in the published 0.2.0. They are not "future
work" — they mean a requirements.txt with an inline comment (extremely common)
currently produces a wrong answer attributed to the user's repo.
