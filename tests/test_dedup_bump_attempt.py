"""bump_attempt атомарен: N одновременных инкрементов одного ключа дают ровно N.

До v0.3.1 — SELECT, затем UPDATE или INSERT без ON CONFLICT: одно обновление терялось,
а второй INSERT падал UniqueViolation (вызывающий глотал — счётчики занижались молча).
"""
import threading
import time

import pytest

from tekstura_core import dedup


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(dedup, "_IS_PG", False)
    monkeypatch.setattr(dedup, "_PH", "?")
    monkeypatch.setattr(dedup, "_PATH", str(tmp_path / "dedup.sqlite3"))
    dedup.init_db()
    return dedup


def test_sequential_bumps_count_up(db):
    assert [db.bump_attempt("k") for _ in range(3)] == [1, 2, 3]
    assert db.attempt_info("k")[0] == 3


def test_concurrent_bumps_are_not_lost(db, monkeypatch):
    """Барьер на N потоков + задержка внутри транзакции: окно гонки открыто настежь."""
    n = 8
    barrier = threading.Barrier(n)
    real_conn = db._conn

    class SlowConn:
        """Соединение, которое задерживает КАЖДЫЙ запрос — так SELECT и UPDATE расходятся."""

        def __init__(self):
            self._c = real_conn()

        def execute(self, *a, **k):
            cur = self._c.execute(*a, **k)
            time.sleep(0.02)
            return cur

        def commit(self):
            self._c.commit()

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *exc):
            out = self._c.__exit__(*exc)
            self._c.close()
            return out

    monkeypatch.setattr(db, "_conn", SlowConn)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(db.bump_attempt("letter:rejection"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert sorted(results) == list(range(1, n + 1)), results
    assert db.attempt_info("letter:rejection")[0] == n
