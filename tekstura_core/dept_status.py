"""Штаб отдела ИИ — общая точка статусов агентов (Кант/Milica/Ivana/…).

Каждый агент периодически пишет сюда компактный статус (report): жив ли, что сделал, что в очереди,
что болит, сколько сжёг. Любой читатель (Кант в личке Дмитрию, дневной отчёт, будущий Nikola-штаб)
берёт сводку за день (read_all) или историю одного агента (read_agent) — без хождения по чужим БД
и логам сервисов.

БД-абстракция та же, что в dedup.py (Postgres на Railway / sqlite локально, выбор по DATABASE_URL),
с ОДНИМ намеренным отличием: URL резолвится на КАЖДОМ вызове, а не на import-time. Почему: dedup
фиксирует бэкенд при импорте — офлайн-тест (и любой скрипт с подменой DATABASE_URL) уже не может
подсунуть tmp-sqlite, потому что conftest успел прогрузить app.config с .env Канта. Таблица здесь
одна, вызовы редкие (раз в час на агента) — цена per-call резолва нулевая, тестируемость полная.

«День» — Белград (config.TZ_NAME), как у claim_daily_* в dedup: гранулярность статусов обязана
совпадать с бизнес-днём, иначе около полуночи статус уезжает на «вчера/завтра». Метки времени
(updated_at) — UTC ISO, конвенция репо.

Ключ таблицы — (agent, day): агенты в ОБЩЕЙ штабной БД не толкаются (в отличие от brain_spend
в dedup, который ключуется только днём — потому там «своя БД на сервис» обязательна).
Никаких внешних зависимостей: stdlib + app.config (psycopg импортируется лениво и только под PG).
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS dept_status (
    agent TEXT, day TEXT, status_json TEXT, updated_at TEXT,
    PRIMARY KEY (agent, day))"""


def _db_url() -> str:
    # ШТАБ — ОБЩИЙ на весь отдел, а DATABASE_URL у агентов РАЗНЫЙ (у Milica свой Postgres в
    # другом Railway-проекте): DEPT_STATUS_DATABASE_URL указывает на общую штабную БД и имеет
    # высший приоритет (конвенция 04.07, подключение Milica к штабу Nicola). Без него — как
    # раньше: env DATABASE_URL (тесты/скрипты подменяют позже import-time) → config.
    return (os.getenv("DEPT_STATUS_DATABASE_URL", "")
            or os.getenv("DATABASE_URL", "")
            or config.DATABASE_URL)


def _connect():
    """(conn, placeholder) под ТЕКУЩИЙ DATABASE_URL. Плейсхолдер разный: psycopg '%s', sqlite '?'."""
    url = _db_url()
    if url.startswith("postgres"):
        import psycopg  # ленивый импорт: локально/в тестах psycopg может отсутствовать
        return psycopg.connect(url), "%s"
    return sqlite3.connect(url.replace("sqlite:///", "")), "?"


def _today() -> str:
    """Сегодня по Белграду (бизнес-день, как claim_daily_* в dedup)."""
    return datetime.now(ZoneInfo(config.TZ_NAME)).date().isoformat()


def init_db() -> None:
    conn, _ph = _connect()
    try:
        with conn:                    # commit на выходе блока (и psycopg, и sqlite3)
            conn.execute(_TABLE_SQL)
    finally:
        conn.close()                  # psycopg закрывает сам в with, sqlite3 — нет; close идемпотентен


def report(agent: str, payload: dict) -> None:
    """Upsert статуса агента ЗА СЕГОДНЯ (день — Белград). payload — компактный dict (что сделал,
    очередь, ошибки, траты); сериализуется в JSON. Повторный report в тот же день ПЕРЕЗАПИСЫВАЕТ
    (агент шлёт нарастающий итог дня, а не журнал — журналы у каждого свои).

    Fail-open по образцу budget_milica: штаб — вспомогательный контур, сбой штабной БД НЕ должен
    ронять тик агента (иначе отчётность валит прод — направление отказа было бы перепутано)."""
    try:
        _write(str(agent), _today(), payload)
    except Exception as e:  # noqa: BLE001 — fail-open, см. докстринг
        print(f"[dept] report({agent}) не записался: {e}", flush=True)


def _write(agent: str, day: str, payload: dict, now: str | None = None) -> None:
    """Низкоуровневый писатель (день — параметром). report() всегда пишет «сегодня»; произвольный
    день нужен тестам и бэкфиллу. ON CONFLICT по PK (agent, day) — идемпотентно при деплой-оверлапе
    Railway (два воркера на одной БД), как mark_message в dedup."""
    now = now or datetime.now(timezone.utc).isoformat()
    body = json.dumps(payload or {}, ensure_ascii=False)
    conn, ph = _connect()
    try:
        with conn:
            conn.execute(
                f"INSERT INTO dept_status VALUES ({ph},{ph},{ph},{ph}) "
                "ON CONFLICT (agent, day) DO UPDATE SET "
                "status_json=excluded.status_json, updated_at=excluded.updated_at",
                (str(agent), str(day), body, now))
    finally:
        conn.close()


def _loads(body) -> dict:
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001 — битый JSON не должен прятать агента из сводки штаба
        return {"_raw": body}


def read_all(day: str | None = None) -> dict:
    """Сводка статусов ВСЕХ агентов за день → {agent: payload}. day=None → сегодня (Белград)."""
    d = day or _today()
    conn, ph = _connect()
    try:
        cur = conn.execute(f"SELECT agent, status_json FROM dept_status WHERE day={ph}", (d,))
        return {agent: _loads(body) for agent, body in cur.fetchall()}
    finally:
        conn.close()


def read_agent(agent: str, days: int = 7) -> list[dict]:
    """История ОДНОГО агента за последние `days` дней (включая сегодня), НОВЫЕ→СТАРЫЕ:
    [{"day", "payload", "updated_at"}, …]. Отсечка по ISO-строке дня — лексикографический порядок
    совпадает с календарным, отдельного парсинга дат не нужно."""
    cutoff = (datetime.now(ZoneInfo(config.TZ_NAME)).date()
              - timedelta(days=max(int(days), 1) - 1)).isoformat()
    conn, ph = _connect()
    try:
        cur = conn.execute(
            f"SELECT day, status_json, updated_at FROM dept_status "
            f"WHERE agent={ph} AND day>={ph} ORDER BY day DESC",
            (str(agent), cutoff))
        return [{"day": day, "payload": _loads(body), "updated_at": upd}
                for day, body, upd in cur.fetchall()]
    finally:
        conn.close()
