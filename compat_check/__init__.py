"""compat-check — dry-run dependency compatibility probing.

`__version__` is the single source of truth for the package version:
pyproject.toml reads it from here via setuptools' dynamic version, so the
installed metadata and the cache key can never disagree about which version
computed a cached answer.
"""

__version__ = "0.3.0"
