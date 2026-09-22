# Design — fetcher v0.3: complete the requirement sources

Status: proposed, not implemented. Written after measuring the current
fetcher's real behaviour (see "Measured, not assumed"), not from the ledger's
deferred-risk list alone.

**REVISED** after benchmarking against pip's `req_file.py` and setuptools'
`config/expand.py` — see `benchmark-fetcher-v0.3.md`. The benchmark added a
**Unit 0** (requirement-line parsing) covering three defects live in the
shipped 0.2.0, and replaced Unit 1's cycle guard with pip's per-branch dict.
Read the benchmark alongside this document; where they disagree, the benchmark
wins, because it was checked against a reference implementation.

## The defect class this addresses

`fetch_requirements()` has one contract that matters: **it either returns the
complete requirement list, or it raises.** It must never return a partial list,
because every caller downstream treats what it returns as the whole truth —
`probe_all()` will report "OK, 3 packages would install cleanly" for a list that
was silently missing half its entries.

Two of the four gaps break that contract. Two do not. They deserve different
priority, and the ledger's flat "deferred risk" list obscured that:

| Gap | Breaks the contract? | Failure mode |
|---|---|---|
| `-r nested.txt` skipped | **YES** | silently partial → confident wrong answer |
| `-e .` skipped | **YES** (sometimes) | same, when `.` carries the real deps |
| `setup.py` unsupported | no | raises FetchError — honest failure |
| branch / error diagnosis | no | raises FetchError — honest but unhelpful |

So the nested-include work is a **correctness fix**; the other two are
**coverage** and **ergonomics**. Sequenced accordingly.

## Measured, not assumed

Run against the live network before designing:

1. **Silent truncation confirmed.** A requirements.txt containing
   `-r base.txt`, `requests>=2.0`, `-e .`, `flask`, `--index-url ...`, `numpy`
   parses to exactly `['requests>=2.0', 'flask', 'numpy']`. `base.txt` is never
   fetched, and the caller is handed that list as if complete.

2. **`develop`-default repos are rarer than assumed.** Both `django/django`
   and `pallets/flask` report `default_branch = main`. The original
   justification for branch detection ("repos using develop") is weak. The real
   justification is different — see 3.

3. **A 404 does not mean what the code thinks it means.** Probing
   `jahyunlee00299/compat-check` — a repo that is **public and exists** — at
   `main/pyproject.toml` returns 404, because its default branch is `master`.
   The current code does recover (it falls back to `master`), but burns a 404
   round-trip per candidate file first, and a genuine "repo does not exist" is
   indistinguishable from "wrong branch guess" in the resulting error text.
   Branch detection is therefore about **correct diagnosis and fewer
   round-trips**, not exotic branch names.

4. **`setup.py`-only repos are real.** `benjaminp/six` has `setup.py` and
   `setup.cfg`, no `pyproject.toml`, no `requirements.txt`. Note the existing
   `setup.cfg` parser already covers `six` — the genuinely uncovered case is a
   repo carrying `setup.py` alone.

5. **URL coverage is narrower than it looks.** `parse_github_url()` returns
   `None` for `git@github.com:psf/requests.git`, for a scheme-less
   `github.com/psf/requests`, and for a `/blob/<branch>/<file>` deep link. A
   `None` falls through to the **PyPI** path, so `github.com/psf/requests` is
   currently looked up as a PyPI package of that literal name and fails with a
   misleading "PyPI package not found". This was not on the deferred-risk list
   at all.

## Unit 1 — `-r` includes and `-e .` (correctness)

**Goal**: never return a partial list. Either resolve every include, or raise.

- `_parse_requirements_txt(text)` becomes `_parse_requirements_txt(text, resolver)`
  where `resolver(relative_path) -> str | None` fetches a sibling file. The
  parser stays pure; the network stays in `_fetch_from_github`. This preserves
  the existing tests' ability to call the parser with a plain string.
- `-r other.txt` / `--requirement other.txt` → resolve relative to the current
  file's directory, parse recursively, splice in.
- Cycle guard: **pip's per-branch dict**, `{abspath: first_including_file}`,
  copied on each descent — not a shared `seen` set. A set cannot distinguish
  "already included via another path" (a legal diamond) from a true cycle, and
  this unit declares an unresolvable include fatal, so that distinction is
  load-bearing. See benchmark §5.
- Depth cap (8) and total-file cap (32), so a pathological repo cannot turn one
  CLI call into hundreds of HTTP requests.
- An include that 404s is **fatal**, not skipped. A missing `base.txt` means we
  cannot know the real requirement set, and guessing is precisely the failure
  this unit exists to remove.
- `-e .` and bare `.` mean the repo's own package: resolve by re-entering the
  normal candidate-file search for that directory. `-e git+https://...` is a
  *different* project and is **not** followed — it is recorded as an unresolved
  external, and raises if it would change the answer.
- `--index-url`, `--extra-index-url`, `--find-links`, `-c constraints.txt`
  remain skipped, but skipping is now **recorded and surfaced**, because `-c`
  genuinely constrains resolution.

**Prove**: fixture tree (`dev.txt` including `base.txt`) through a fake
resolver; a real repo whose requirements.txt uses `-r`.
**Refute**: cycle terminates; a missing include raises instead of truncating;
the depth cap trips; the 6-line sample above now yields the *complete* set
rather than 3 entries.

## Unit 2 — `setup.py` via AST (coverage)

**Goal**: read `install_requires` **without executing the file**.

- `ast.parse()`, walk to the `setup(...)` call, read the `install_requires`
  keyword. Accept a list of string literals, and a name bound earlier in the
  module to a list literal (the common `REQUIRES = [...]` shape).
- Anything computed at runtime (comprehension, file read, function call,
  conditional) is not guessable. **Do not guess.** Raise a FetchError stating
  that requirements are computed dynamically and naming the file — an honest
  failure, same contract as Unit 1.
- Never `exec`/`import` the file. Executing a stranger's `setup.py` on the host
  is the one thing this tool must not do. (The ledger's existing accepted risk
  covers build hooks running *inside a disposable venv* — a far narrower
  exposure than host execution.)
- Ordering: `setup.py` goes **after** `setup.cfg` in the candidate list, since a
  declarative source beats an AST guess.

**Prove**: a `six`-shaped fixture; a literal-list setup.py; a name-bound-list
setup.py.
**Refute**: dynamic `install_requires` raises clearly instead of returning `[]`
or a partial list; a setup.py containing `os.system(...)` parses without
executing anything — asserted via a sentinel file that must not appear.

## Unit 3 — default branch + error diagnosis (ergonomics)

**Goal**: stop guessing the branch; stop reporting every failure identically.

- Resolve the default branch once via `api.github.com/repos/{owner}/{repo}`
  before fetching files. One request replaces up to N wasted 404s.
- That same call distinguishes cases the current code collapses:
  - 200 → repo exists; use its `default_branch`
  - 404 → repo does not exist **or** is private (GitHub returns 404 for both,
    deliberately, to avoid leaking existence). Say exactly that; mention
    private repos are unsupported. Do **not** claim the repo does not exist.
  - 403 with `X-RateLimit-Remaining: 0` → rate limited, with the reset time.
    Today this surfaces as a bare `HTTP 403`.
- Keep the unauthenticated path. If the API call fails for any reason, fall back
  to the existing `main` → `master` probing rather than hard-failing: the API is
  an optimization and a diagnostic, not a new dependency.
- Extend `parse_github_url()` to accept `git@github.com:o/r.git`, scheme-less
  `github.com/o/r`, and `/blob/<branch>/<path>` deep links (use the branch,
  ignore the file path). Anything still unparsed that *contains* `github.com`
  must raise a GitHub-specific error rather than silently falling through to
  the PyPI lookup.

**Prove**: each URL form parses to the right ref; the public repo whose default
branch is `master` resolves with zero 404s.
**Refute**: API unreachable (mocked) still resolves via the old fallback; a
private/nonexistent repo yields the both-cases message, not a false claim; a
`github.com/...` string never reaches the PyPI path.

## Cross-cutting: FetchResult instead of a bare list

Units 1 and 3 both need to report things a bare `list[str]` cannot carry: which
file the requirements came from, which includes were followed, what was
deliberately skipped.

Proposed: `fetch_requirements()` keeps returning `list[str]` (no caller breaks),
and a new `fetch_requirements_detailed() -> FetchResult` carries provenance. The
CLI uses the detailed form to print a `source:` line and any skip warnings —
the same pattern `ResolvedParams` established in 0.2.0: **state what was
actually used; never let an ignored input pass silently.**

## Sequencing

0. **Unit 0** (added by the benchmark) — requirement-line parsing: `\`
   continuations, trailing comments, the explicit option table, `${VAR}`
   detection. Three of these are live defects in 0.2.0, so this ships first.
1. **Unit 1** — correctness; silent truncation is the only *design-level* gap
   that returns a wrong answer.
2. **Unit 3** — diagnosis; cheap, and makes Unit 2's failures legible.
3. **Unit 2** — coverage; largest surface, smallest correctness impact.
4. **FetchResult + CLI provenance**, folded in as Units 1 and 3 require it.

Version target: 0.3.0.
