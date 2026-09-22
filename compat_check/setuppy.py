"""Read ``install_requires`` from a ``setup.py`` WITHOUT executing it.

Why not just import it
----------------------
setuptools solves this same problem in ``config/expand.py`` with a
``StaticModule`` that AST-parses the file and ``ast.literal_eval``s the
assignment — the approach used here. But its ``read_attr()`` ends with:

    except Exception:              # intentional broad fallback
        module = _load_spec(spec, module_name)   # imports and EXECUTES
        return getattr(module, attr_name)

setuptools may do that: it is building *your own* project, which you already
trust. compat-check reads arbitrary third-party repositories over the network,
so executing their ``setup.py`` on the host is the one thing it must not do.
This module therefore has **no execution fallback at all**: when a value is
not statically knowable it raises, and the caller reports an honest failure
rather than a guessed or partial list.

What is actually out there
--------------------------
Surveyed 20 well-known repos that still ship a ``setup.py``. Of the 5 that
pass ``install_requires`` to ``setup()``, 3 are statically resolvable and 2
are not:

* ``Name`` bound to a module-level list literal — the most common form
  (records, boto3), and the reason a bare-literal-only parser would miss most
  real cases
* a list literal inline (supervisor — an empty one)
* ``Call`` (celery) and ``BinOp`` (gevent) — not knowable without running the
  file; these raise.
"""
from __future__ import annotations

import ast


class SetupPyError(Exception):
    """setup.py could not be read statically. Never a partial answer."""


def _module_level_bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Names assigned at module level, mapped to their value expression.

    Only top-level assignments: a name rebound inside a function or an ``if``
    is not reliably the value ``setup()`` receives, and guessing which branch
    runs is exactly the kind of inference this module refuses to make.
    """
    bindings: dict[str, ast.expr] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value:
            if isinstance(statement.target, ast.Name):
                bindings[statement.target.id] = statement.value
    return bindings


def _find_setup_call(tree: ast.Module) -> ast.Call | None:
    """The ``setup(...)`` call, whether imported bare or used as ``setuptools.setup``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "setup":
            return node
    return None


def _as_requirement_list(value: ast.expr, bindings: dict[str, ast.expr], what: str) -> list[str]:
    """Evaluate ``value`` to a list of strings, statically or not at all."""
    if isinstance(value, ast.Name):
        bound = bindings.get(value.id)
        if bound is None:
            raise SetupPyError(
                f"{what} is the variable {value.id!r}, which is not assigned at "
                f"module level; its value cannot be determined without running "
                f"setup.py"
            )
        value = bound

    try:
        evaluated = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        raise SetupPyError(
            f"{what} is computed at runtime ({type(value).__name__}); "
            f"compat-check does not execute setup.py, so the requirement list "
            f"cannot be determined. A pyproject.toml or requirements.txt would "
            f"be readable."
        ) from None

    if not isinstance(evaluated, (list, tuple)):
        raise SetupPyError(f"{what} is not a list (got {type(evaluated).__name__})")

    requirements = []
    for item in evaluated:
        if not isinstance(item, str):
            raise SetupPyError(
                f"{what} contains a non-string entry ({item!r}); the list is "
                f"not a plain requirement list"
            )
        item = item.strip()
        if item:
            requirements.append(item)
    return requirements


def parse_extras_require(text: str, extras: list[str]) -> list[str]:
    """Return the requirements that ``extras`` select from ``extras_require``.

    ``-e .[pg]`` genuinely requires what ``extras_require["pg"]`` lists
    (records' ``pg`` is ``['psycopg2-binary']``), so resolving only the base
    dependencies checks a smaller set than the project asked for.

    An extra that is declared but computed at runtime, or not declared at all,
    raises — the caller then reports a stated gap rather than a short list.
    """
    if not extras:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise SetupPyError(f"setup.py is not valid Python: {e}") from e

    call = _find_setup_call(tree)
    if call is None:
        raise SetupPyError("no setup() call found in setup.py")

    bindings = _module_level_bindings(tree)
    value = None
    for keyword in call.keywords:
        if keyword.arg == "extras_require":
            value = keyword.value
            break
    if value is None:
        raise SetupPyError("setup() declares no extras_require")

    if isinstance(value, ast.Name):
        bound = bindings.get(value.id)
        if bound is None:
            raise SetupPyError(
                f"extras_require is the variable {value.id!r}, which is not "
                f"assigned at module level"
            )
        value = bound

    try:
        mapping = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        raise SetupPyError(
            f"extras_require is computed at runtime ({type(value).__name__})"
        ) from None

    if not isinstance(mapping, dict):
        raise SetupPyError("extras_require is not a dict")

    collected: list[str] = []
    for extra in extras:
        if extra not in mapping:
            raise SetupPyError(
                f"extra {extra!r} is not declared in extras_require "
                f"(declared: {', '.join(sorted(map(str, mapping))) or 'none'})"
            )
        entries = mapping[extra]
        if not isinstance(entries, (list, tuple)):
            raise SetupPyError(f"extras_require[{extra!r}] is not a list")
        for item in entries:
            if not isinstance(item, str):
                raise SetupPyError(
                    f"extras_require[{extra!r}] contains a non-string entry"
                )
            item = item.strip()
            if item:
                collected.append(item)
    return collected


def parse_setup_py(text: str) -> list[str]:
    """Return ``install_requires`` from ``text``, or raise SetupPyError.

    An empty list is a real answer — "this project declares no dependencies" —
    and is distinct from a failure, which always raises. Nothing in this
    function imports, compiles or executes the parsed source.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise SetupPyError(f"setup.py is not valid Python: {e}") from e

    call = _find_setup_call(tree)
    if call is None:
        raise SetupPyError("no setup() call found in setup.py")

    bindings = _module_level_bindings(tree)

    for keyword in call.keywords:
        if keyword.arg == "install_requires":
            return _as_requirement_list(keyword.value, bindings, "install_requires")
        if keyword.arg is None:
            # setup(**kwargs) — the arguments live in a dict we would have to
            # execute the file to see.
            raise SetupPyError(
                "setup() is called with **kwargs, so its arguments cannot be "
                "read without running setup.py"
            )

    raise SetupPyError("setup() declares no install_requires")
