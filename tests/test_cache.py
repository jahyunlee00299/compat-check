"""Integration tests for compat_check.cache — real probe_all() calls, real SQLite file."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check.cache import _evict_if_needed, _connect, cached_probe_all, make_cache_key
from compat_check.params import resolve_params


def test_cache_key_ignores_requirement_order():
    params = resolve_params("3.11", "pip")
    a = make_cache_key(["requests>=2.0", "numpy==1.0"], params)
    b = make_cache_key(["numpy==1.0", "requests>=2.0"], params)
    assert a == b


def test_cache_key_differs_by_python_version_on_a_backend_that_honors_it():
    # uv, not pip: the pip fallback cannot target another version, so on pip
    # these two MUST collapse instead (covered in tests/test_params.py).
    a = make_cache_key(["requests>=2.0"], resolve_params("3.11", "uv"))
    b = make_cache_key(["requests>=2.0"], resolve_params("3.12", "uv"))
    assert a != b


def test_eviction_drops_oldest_rows_past_the_cap():
    with tempfile.TemporaryDirectory(prefix="compat_check_evict_") as tmp:
        db_path = Path(tmp) / "history.db"
        conn = _connect(db_path)
        try:
            # checked_at ascending: row 0 is the oldest.
            for i in range(10):
                conn.execute(
                    "INSERT INTO probe_cache (cache_key, ok, failures_json, "
                    "resolved_json, backend, checked_at) VALUES (?,?,?,?,?,?)",
                    (f"key{i}", 1, "[]", "[]", "uv", 1000.0 + i),
                )
            removed = _evict_if_needed(conn, max_rows=4)
            assert removed == 6, removed

            keys = {r[0] for r in conn.execute("SELECT cache_key FROM probe_cache")}
            assert keys == {"key6", "key7", "key8", "key9"}, keys

            # Below the cap, eviction must be a no-op.
            assert _evict_if_needed(conn, max_rows=4) == 0
        finally:
            conn.close()


def test_a_version_bump_invalidates_existing_rows():
    """A resolver change must not serve answers computed by the old version."""
    import compat_check.params as params_mod

    p = resolve_params("3.11", "uv")
    before = make_cache_key(["requests"], p)
    original = params_mod.__version__
    try:
        params_mod.__version__ = "99.0.0"
        after = make_cache_key(["requests"], p)
    finally:
        params_mod.__version__ = original
    assert before != after


def test_first_call_misses_second_call_hits_and_is_faster():
    with tempfile.TemporaryDirectory(prefix="compat_check_cache_test_") as tmp:
        db_path = Path(tmp) / "history.db"

        t0 = time.monotonic()
        first = cached_probe_all(["numpy==0.0.1"], db_path=db_path)
        first_elapsed = time.monotonic() - t0
        assert first["cache_hit"] is False
        assert first["ok"] is False

        t0 = time.monotonic()
        second = cached_probe_all(["numpy==0.0.1"], db_path=db_path)
        second_elapsed = time.monotonic() - t0
        assert second["cache_hit"] is True
        assert second["ok"] == first["ok"]
        assert second["failures"] == first["failures"]

        # cache hit must be substantially faster than a real dry-run probe
        assert second_elapsed < first_elapsed / 5, (
            f"cache hit ({second_elapsed:.3f}s) not much faster than miss ({first_elapsed:.3f}s)"
        )


def test_expired_entry_forces_a_fresh_probe():
    with tempfile.TemporaryDirectory(prefix="compat_check_cache_test_") as tmp:
        db_path = Path(tmp) / "history.db"

        first = cached_probe_all(["numpy==0.0.1"], db_path=db_path, ttl_seconds=0)
        assert first["cache_hit"] is False

        # ttl_seconds=0 means any elapsed time expires it immediately
        time.sleep(0.01)
        second = cached_probe_all(["numpy==0.0.1"], db_path=db_path, ttl_seconds=0)
        assert second["cache_hit"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(f"running {name}...")
            fn()
            print("  PASS")
    print("\nall tests passed")
