"""Соединения Postgres ядра идут с таймаутами (v0.3.1).

До v0.3.1 `psycopg.connect(DATABASE_URL)` без таймаута запроса и без keepalive: зависший Postgres
или оборванная сеть держали вызывающий поток бесконечно. У Ivana (26.09.2026) такой поток держит
единственного исполнителя кликов — и кнопки не разбирает никто.
"""
import sys
from types import SimpleNamespace

import pytest

from tekstura_core import config, dedup, dept_status


@pytest.fixture
def fake_psycopg(monkeypatch):
    seen = []
    fake = SimpleNamespace(connect=lambda url, **kw: seen.append((url, kw)) or object())
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return seen


def _assert_timeouts(kw):
    assert kw["connect_timeout"] == config.DB_CONNECT_TIMEOUT_SEC
    assert f"statement_timeout={config.DB_STATEMENT_TIMEOUT_MS}" in kw["options"]
    assert kw["keepalives"] == 1 and kw["keepalives_idle"] <= 60
    assert kw["tcp_user_timeout"] > 0


def test_defaults_are_bounded():
    assert config.DB_CONNECT_TIMEOUT_SEC == 10
    assert config.DB_STATEMENT_TIMEOUT_MS == 30000


def test_dedup_connects_with_timeouts(fake_psycopg):
    dedup._pg_connect("postgresql://u@h/db")
    (url, kw), = fake_psycopg
    assert url == "postgresql://u@h/db"
    _assert_timeouts(kw)


def test_dept_status_connects_with_timeouts(fake_psycopg, monkeypatch):
    monkeypatch.setenv("DEPT_STATUS_DATABASE_URL", "postgresql://u@h/dept")
    dept_status._connect()
    (url, kw), = fake_psycopg
    assert url == "postgresql://u@h/dept"
    _assert_timeouts(kw)


def test_zero_statement_timeout_removes_only_the_query_limit(monkeypatch):
    monkeypatch.setattr(config, "DB_STATEMENT_TIMEOUT_MS", 0)
    kw = config.pg_connect_kwargs()
    assert "options" not in kw
    assert kw["connect_timeout"] == config.DB_CONNECT_TIMEOUT_SEC
