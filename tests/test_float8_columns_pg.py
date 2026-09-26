"""Время и деньги на Postgres — DOUBLE PRECISION, а не REAL (float4). Только настоящий Postgres.

До v0.3.2 `sweep_last_run.ts` и `brain_spend.usd` создавались как REAL. На SQLite это 8 байт, на
Postgres — float4: эпоха ~1,79e9 хранится с шагом 128 с, и `claim_interval` сравнивал с
округлённым временем (аудит Иваны 26.09.2026, №1). Набор ядра гоняется на SQLite и этого не видел.

Запуск: TEST_PG_URL=postgresql://postgres@127.0.0.1:55432/<db> pytest tests/test_float8_columns_pg.py
Без TEST_PG_URL тесты пропускаются. Каждый тест работает в своей схеме и сносит её за собой.
"""
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from tekstura_core import dedup

PG_URL = os.getenv("TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="нужен настоящий Postgres: TEST_PG_URL")

# t0 ≡ 63 (mod 128): float4 округляет его ВНИЗ на 63 с — худший случай для гейта.
T0 = 1790451200 - (1790451200 % 128) + 63


def _raw(schema):
    import psycopg
    c = psycopg.connect(PG_URL, autocommit=True)
    c.execute(f"SET search_path TO {schema}")
    return c


@pytest.fixture
def pg(monkeypatch):
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")

    def conn():
        c = dedup._pg_connect(PG_URL)
        c.execute(f"SET search_path TO {schema}")
        c.commit()
        return c

    monkeypatch.setattr(dedup, "_IS_PG", True)
    monkeypatch.setattr(dedup, "_PH", "%s")
    monkeypatch.setattr(dedup, "_conn", conn)
    yield schema
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")


def _types(schema):
    with _raw(schema) as c:
        rows = c.execute(
            "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND (table_name, column_name) IN "
            "(('sweep_last_run', 'ts'), ('brain_spend', 'usd'))", (schema,)).fetchall()
    return dict(rows)


def _freeze(monkeypatch, ts):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(ts, tz or timezone.utc)
    monkeypatch.setattr(dedup, "datetime", Frozen)


def test_fresh_schema_is_double_precision(pg, monkeypatch):
    # Миграция выключена: иначе она маскировала бы откат CREATE обратно на REAL (мутант ревью 26.09).
    monkeypatch.setattr(dedup, "_migrate_float4_columns", lambda: [])
    dedup.init_db()
    assert _types(pg) == {"sweep_last_run.ts": "double precision",
                          "brain_spend.usd": "double precision"}


def test_legacy_real_columns_are_migrated_and_values_kept(pg):
    with _raw(pg) as c:
        c.execute("CREATE TABLE sweep_last_run (name TEXT PRIMARY KEY, ts REAL)")
        c.execute("CREATE TABLE brain_spend (day TEXT PRIMARY KEY, usd REAL)")
        c.execute("INSERT INTO sweep_last_run VALUES ('g', 1790451328)")
        c.execute("INSERT INTO brain_spend VALUES ('2026-09-26', 12.5)")
    dedup.init_db()
    assert _types(pg) == {"sweep_last_run.ts": "double precision",
                          "brain_spend.usd": "double precision"}
    with _raw(pg) as c:
        assert c.execute("SELECT ts FROM sweep_last_run WHERE name='g'").fetchone()[0] == 1790451328
        assert c.execute("SELECT usd FROM brain_spend").fetchone()[0] == 12.5
    # идемпотентно: второй старт ничего не переводит
    assert dedup._migrate_float4_columns() == []


def test_claim_interval_is_exact_on_bad_epoch(pg, monkeypatch):
    """На float4 оба утверждения ниже ложны: t0 сохранялся как t0−63."""
    dedup.init_db()
    _freeze(monkeypatch, T0)
    assert dedup.claim_interval("lock120", 120) is True
    assert dedup.claim_interval("lock60", 60) is True
    _freeze(monkeypatch, T0 + 58)
    assert dedup.claim_interval("lock120", 120) is False   # float4: True уже через 58 с
    _freeze(monkeypatch, T0 + 1)
    assert dedup.claim_interval("lock60", 60) is False     # float4: True через 1 с
    _freeze(monkeypatch, T0 + 121)
    assert dedup.claim_interval("lock120", 120) is True


def test_claim_interval_on_float4_reproduces_the_defect(pg, monkeypatch):
    """Позитивный контроль: на старой REAL-колонке тот же сценарий ломается — тест выше не пустой."""
    with _raw(pg) as c:
        c.execute("CREATE TABLE sweep_last_run (name TEXT PRIMARY KEY, ts REAL)")
    _freeze(monkeypatch, T0)
    assert dedup.claim_interval("lock120", 120) is True
    _freeze(monkeypatch, T0 + 58)
    assert dedup.claim_interval("lock120", 120) is True    # дефект: гейт открылся через 58 с


def test_migration_does_not_hang_on_locked_table(pg, monkeypatch, capsys):
    """Соседний контейнер держит транзакцию на таблице: старт не виснет и не падает."""
    with _raw(pg) as c:
        c.execute("CREATE TABLE sweep_last_run (name TEXT PRIMARY KEY, ts REAL)")
        c.execute("CREATE TABLE brain_spend (day TEXT PRIMARY KEY, usd REAL)")
    monkeypatch.setattr(dedup, "FLOAT4_MIGRATION_LOCK_TIMEOUT", "300ms")
    holder = _raw(pg)
    holder.execute("BEGIN")
    holder.execute("SELECT * FROM sweep_last_run")          # ACCESS SHARE до конца транзакции
    try:
        t = time.monotonic()
        dedup.init_db()                                     # не кидает
        took = time.monotonic() - t
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < 5, took
    types = _types(pg)
    assert types["sweep_last_run.ts"] == "real"             # не взял лок — оставил на следующий старт
    assert types["brain_spend.usd"] == "double precision"   # соседняя колонка переведена
    out = capsys.readouterr().out
    assert "sweep_last_run.ts: перевод в DOUBLE PRECISION не удался (LockNotAvailable" in out, out
    assert dedup._migrate_float4_columns() == ["sweep_last_run.ts"]   # следующий старт доводит


def test_claims_wait_no_longer_than_lock_timeout(pg, monkeypatch):
    """Деплой-оверлап: старый контейнер держит транзакцию, новый шлёт ALTER. Встав в очередь за
    ACCESS EXCLUSIVE, ALTER запирает за собой и гейты третьих сессий — но только до lock_timeout:
    ALTER сдаётся, гейт проходит. Без lock_timeout гейты висели бы, пока держит сосед."""
    with _raw(pg) as c:
        c.execute("CREATE TABLE sweep_last_run (name TEXT PRIMARY KEY, ts REAL)")
        c.execute("INSERT INTO sweep_last_run VALUES ('x', 0)")
    monkeypatch.setattr(dedup, "FLOAT4_MIGRATION_LOCK_TIMEOUT", "1s")
    holder = _raw(pg)
    holder.execute("BEGIN")
    holder.execute("SELECT * FROM sweep_last_run")
    migrated, claimed = [], []
    try:
        alter = threading.Thread(target=lambda: migrated.extend(dedup._migrate_float4_columns()))
        alter.start()
        # Ждём, пока ALTER РЕАЛЬНО встанет в очередь за локом: sleep пропускал случай, когда поток
        # ALTER запаздывал, и тест проходил без очереди вовсе (мутант ревью 26.09).
        deadline = time.monotonic() + 5
        with _raw(pg) as mon:
            while time.monotonic() < deadline:
                if mon.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                               "AND query LIKE 'ALTER TABLE sweep_last_run%%'").fetchone()[0]:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("ALTER так и не встал в очередь за локом")
        t = time.monotonic()
        gate = threading.Thread(target=lambda: claimed.append(dedup.claim_interval("x", 1)))
        gate.start()
        gate.join(10)
        waited = time.monotonic() - t
        alter.join(10)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert claimed == [True]
    assert 0.2 < waited < 3, waited          # стоял в очереди за ALTER'ом, но не за соседом
    assert "sweep_last_run.ts" not in migrated


def test_only_real_columns_are_migrated(pg):
    """Одна колонка уже double — переводится только вторая (мутант ревью: continue → break)."""
    with _raw(pg) as c:
        c.execute("CREATE TABLE sweep_last_run (name TEXT PRIMARY KEY, ts DOUBLE PRECISION)")
        c.execute("CREATE TABLE brain_spend (day TEXT PRIMARY KEY, usd REAL)")
    assert dedup._migrate_float4_columns() == ["brain_spend.usd"]


def test_missing_column_is_logged_not_raised(pg, capsys):
    """Таблиц нет в текущей схеме — не падаем, но и не молчим."""
    assert dedup._migrate_float4_columns() == []
    out = capsys.readouterr().out
    assert "sweep_last_run.ts: колонки нет в текущей схеме" in out, out
    assert "brain_spend.usd: колонки нет в текущей схеме" in out, out
