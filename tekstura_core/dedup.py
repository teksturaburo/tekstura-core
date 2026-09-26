"""Дедупликация и стейт (критично — был инцидент с дублями).

Идемпотентность по message_id + журнал пингов. Postgres на Railway, sqlite локально.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from . import config

_IS_PG = config.DATABASE_URL.startswith("postgres")

if _IS_PG:
    import psycopg
    def _conn():
        return psycopg.connect(config.DATABASE_URL)
    _PH = "%s"
else:
    _PATH = config.DATABASE_URL.replace("sqlite:///", "")
    def _conn():
        return sqlite3.connect(_PATH)
    _PH = "?"


def init_db() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY, dialog_id TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ping_log (
            task_id TEXT, user_id TEXT, sent_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS digest_log (
            created_at TEXT, day TEXT, requester_id TEXT, requester_name TEXT,
            dialog_id TEXT, text TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS digest_sent (day TEXT PRIMARY KEY, sent_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS boss_requests (
            task_id TEXT PRIMARY KEY, requester_dialog TEXT, requester_name TEXT,
            title TEXT, created_at TEXT, relayed TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS message_attempts (
            message_id TEXT PRIMARY KEY, attempts INTEGER, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS brain_spend (day TEXT PRIMARY KEY, usd REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS budget_alert (day TEXT PRIMARY KEY, sent_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS credit_alert (day TEXT PRIMARY KEY, sent_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_report_sent (day TEXT PRIMARY KEY, sent_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_audit_done (day TEXT PRIMARY KEY, done_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_nudge_audit_done (day TEXT PRIMARY KEY, done_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS production_sweep_done (day TEXT PRIMARY KEY, done_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS task_stage_map (scope TEXT PRIMARY KEY, data TEXT, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sweep_last_run (name TEXT PRIMARY KEY, ts REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS email_drafts (
            draft_id TEXT PRIMARY KEY, to_email TEXT, subject TEXT, body TEXT,
            created_at TEXT, sent_at TEXT, approved_at TEXT, approved_by TEXT)""")
        if not _IS_PG:
            c.commit()
    _migrate_email_draft_columns()


def _migrate_email_draft_columns() -> None:
    """Идемпотентно добавить колонки апрува в СУЩЕСТВУЮЩУЮ email_drafts (прод-БД, созданную до гейта
    апрува P1). Каждый ALTER — в СВОЕЙ транзакции: на Postgres упавший ALTER (колонка уже есть)
    отравил бы общую транзакцию init_db и уронил остальные CREATE — здесь же ловится изолированно."""
    for col in ("approved_at", "approved_by"):
        try:
            with _conn() as c:
                c.execute(f"ALTER TABLE email_drafts ADD COLUMN {col} TEXT")
                if not _IS_PG:
                    c.commit()
        except Exception:  # noqa: BLE001 — колонка уже есть (sqlite/pg) → миграция не нужна
            pass


def attempt_info(message_id: str) -> tuple[int, str | None]:
    """(attempts, updated_at_iso) без инкремента — для backoff-ретрая (когда ретраить, когда сдаться)."""
    with _conn() as c:
        cur = c.execute(f"SELECT attempts, updated_at FROM message_attempts WHERE message_id={_PH}",
                        (str(message_id),))
        row = cur.fetchone()
    return (int(row[0] or 0), row[1]) if row else (0, None)


def bump_attempt(message_id: str) -> int:
    """Увеличить счётчик попыток обработки сообщения, вернуть новое значение.

    АТОМАРНО (v0.3.1): один UPSERT … RETURNING. До этого — SELECT, потом UPDATE или INSERT без
    ON CONFLICT: при одновременном инкременте одного ключа (второй поток агента, деплой-оверлап
    двух контейнеров) одно обновление терялось, а INSERT второго падал UniqueViolation. У Иваны
    так занижались счётчики суточной сводки — исключение глоталось вызывающим.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO message_attempts (message_id, attempts, updated_at) VALUES ({_PH}, 1, {_PH}) "
            f"ON CONFLICT (message_id) DO UPDATE SET "
            f"attempts = COALESCE(message_attempts.attempts, 0) + 1, updated_at = excluded.updated_at "
            f"RETURNING attempts",
            (str(message_id), now))
        n = int(cur.fetchone()[0])
        if not _IS_PG:
            c.commit()
        return n


def add_boss_request(task_id, requester_dialog, requester_name, title) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        try:
            c.execute(f"INSERT INTO boss_requests VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH})",
                      (str(task_id), str(requester_dialog), requester_name or "", (title or "")[:240], now, ""))
        except Exception:  # noqa: BLE001 — уже есть
            pass
        if not _IS_PG:
            c.commit()


def open_boss_requests() -> list[dict]:
    with _conn() as c:
        cur = c.execute("SELECT task_id, requester_dialog, requester_name, title FROM boss_requests "
                        f"WHERE relayed IS NULL OR relayed={_PH}", ("",))
        return [{"task_id": r[0], "requester_dialog": r[1], "requester_name": r[2], "title": r[3]}
                for r in cur.fetchall()]


def mark_boss_relayed(task_id) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"UPDATE boss_requests SET relayed={_PH} WHERE task_id={_PH}", (now, str(task_id)))
        if not _IS_PG:
            c.commit()


def log_for_digest(day: str, requester_id, requester_name: str, dialog_id: str, text: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO digest_log VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH})",
                  (now, day, str(requester_id), requester_name or "", str(dialog_id), text or ""))
        if not _IS_PG:
            c.commit()


def digest_items(day: str) -> list[dict]:
    with _conn() as c:
        cur = c.execute(
            f"SELECT requester_id, requester_name, text, created_at FROM digest_log WHERE day={_PH} ORDER BY created_at",
            (day,))
        return [{"requester_id": r[0], "requester_name": r[1], "text": r[2], "created_at": r[3]}
                for r in cur.fetchall()]


def digest_already_sent(day: str) -> bool:
    with _conn() as c:
        cur = c.execute(f"SELECT 1 FROM digest_sent WHERE day={_PH}", (day,))
        return cur.fetchone() is not None


def mark_digest_sent(day: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO digest_sent VALUES ({_PH},{_PH})", (day, now))
        if not _IS_PG:
            c.commit()


def claim_daily_report(day: str) -> bool:
    """Атомарно «застолбить» дневной отчёт за day. True = ИМЕННО ЭТОТ вызов застолбил (надо слать),
    False = уже застолблено (другим тиком/инстансом при деплой-оверлапе) → не дублируем отчёт.
    ON CONFLICT DO NOTHING + rowcount: гонка двух воркеров на одной БД разрешается в пользу одного."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(f"INSERT INTO daily_report_sent VALUES ({_PH},{_PH}) "
                        f"ON CONFLICT (day) DO NOTHING", (day, now))
        won = cur.rowcount == 1
        if not _IS_PG:
            c.commit()
        return won


def claim_daily_audit(day: str) -> bool:
    """Атомарно застолбить ночной аудит за day (анти-двойной запуск при деплой-оверлапе). True = этот вызов
    застолбил → надо гнать аудит; False = уже застолблено."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(f"INSERT INTO daily_audit_done VALUES ({_PH},{_PH}) "
                        f"ON CONFLICT (day) DO NOTHING", (day, now))
        won = cur.rowcount == 1
        if not _IS_PG:
            c.commit()
        return won


def claim_daily_nudge_audit(day: str) -> bool:
    """Атомарно застолбить ЕЖЕДНЕВНЫЙ нудж-аудит (дубли/прочитка) за day. True = этот вызов застолбил."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(f"INSERT INTO daily_nudge_audit_done VALUES ({_PH},{_PH}) "
                        f"ON CONFLICT (day) DO NOTHING", (day, now))
        won = cur.rowcount == 1
        if not _IS_PG:
            c.commit()
        return won


def claim_production_sweep(day: str) -> bool:
    """Атомарно застолбить ночной обход Production за day (анти-двойной запуск при деплой-оверлапе).
    True = ИМЕННО этот вызов застолбил → гнать обход; False = уже застолблено другим тиком/инстансом."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(f"INSERT INTO production_sweep_done VALUES ({_PH},{_PH}) "
                        f"ON CONFLICT (day) DO NOTHING", (day, now))
        won = cur.rowcount == 1
        if not _IS_PG:
            c.commit()
        return won


def get_task_stage_map(scope: str) -> dict:
    """Последняя виденная стадия каждой задачи в scope (напр. группе): {task_id: stage_id}.
    Хранится одной JSON-строкой — один SELECT/UPSERT на опрос вместо N запросов."""
    with _conn() as c:
        cur = c.execute(f"SELECT data FROM task_stage_map WHERE scope={_PH}", (scope,))
        row = cur.fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return {}


def set_task_stage_map(scope: str, mapping: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    data = json.dumps(mapping)
    with _conn() as c:
        c.execute(f"INSERT INTO task_stage_map (scope, data, updated_at) VALUES ({_PH},{_PH},{_PH}) "
                  f"ON CONFLICT (scope) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                  (scope, data, now))
        if not _IS_PG:
            c.commit()


def claim_interval(name: str, min_sec: float) -> bool:
    """ПЕРСИСТЕНТНЫЙ интервальный гейт (переживает рестарт, в отличие от time.monotonic, который сбрасывается на
    старте → дайджест повторялся на КАЖДОМ деплое). True = с прошлого запуска `name` прошло ≥ min_sec (и застолбили
    now); False = ещё рано. Анти-спам эскалаций Дмитрию при частых рестартах (Дмитрий 02.07: «чуть пореже»).

    АТОМАРНО (аудит F07): единый UPSERT с условием + rowcount вместо SELECT→DELETE→INSERT. Прежний
    неатомарный вариант при деплой-оверлапе Railway (два контейнера на одной БД) давал обоим TRUE в
    READ COMMITTED — свипы бежали параллельно и дублировали действия. Теперь ровно один воркер
    выигрывает застолбление (как claim_daily_report)."""
    now = datetime.now(timezone.utc).timestamp()
    threshold = now - float(min_sec)
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO sweep_last_run (name, ts) VALUES ({_PH},{_PH}) "
            f"ON CONFLICT (name) DO UPDATE SET ts=excluded.ts "
            f"WHERE sweep_last_run.ts < {_PH}",
            (str(name), now, threshold))
        won = cur.rowcount == 1
        if not _IS_PG:
            c.commit()
        return won


def claim_once(key: str) -> bool:
    """Атомарно застолбить УНИКАЛЬНЫЙ ключ (одноразовое действие: «одно письмо этого рода на этот
    адрес», аудит F25). True = ИМЕННО этот вызов застолбил; False = уже застолблено. Реюзает
    processed_messages (message_id — PK). КИДАЕТ при сбое БД — вызывающий обязан fail-closed (для
    писем наружу молчание безопаснее дубля). Отличие от seen_message+mark_message: атомарно и без
    окна TOCTOU между проверкой и меткой."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(f"INSERT INTO processed_messages VALUES ({_PH},{_PH},{_PH}) "
                        f"ON CONFLICT (message_id) DO NOTHING", (str(key), "claim-once", now))
        won = cur.rowcount == 1
        if not _IS_PG:
            c.commit()
        return won


# --- Персистентный стор черновиков писем кандидатам (аудит F20/F03) ------------------------------------
# Апрув письма Дмитрием живёт МЕЖДУ ходами мозга (мозг stateless), а «после апрува текст не менять
# ни на символ» невыполнимо без хранилища. Решение: черновик кладём в БД, наружу уходит ИМЕННО
# сохранённый (Дмитрием виденный) текст — модель на отправке не пересказывает тело (закрывает и
# промпт-инъекцию: send_approved_email больше не принимает произвольный body от мозга).

def save_email_draft(draft_id: str, to_email: str, subject: str, body: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO email_drafts (draft_id, to_email, subject, body, created_at, sent_at) "
                  f"VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH}) "
                  f"ON CONFLICT (draft_id) DO UPDATE SET to_email=excluded.to_email, "
                  f"subject=excluded.subject, body=excluded.body, created_at=excluded.created_at",
                  (str(draft_id), str(to_email).strip().lower(), subject or "", body or "", now, None))
        if not _IS_PG:
            c.commit()


def get_pending_draft(to_email: str, draft_id: str = "") -> "dict | None":
    """Последний НЕотправленный черновик для адреса (или конкретный по draft_id). None — если нет."""
    to_email = str(to_email or "").strip().lower()
    with _conn() as c:
        if str(draft_id or "").strip():
            cur = c.execute(f"SELECT draft_id, to_email, subject, body, approved_at, approved_by "
                            f"FROM email_drafts WHERE draft_id={_PH} AND sent_at IS NULL",
                            (str(draft_id),))
        else:
            cur = c.execute(f"SELECT draft_id, to_email, subject, body, approved_at, approved_by "
                            f"FROM email_drafts WHERE to_email={_PH} AND sent_at IS NULL "
                            f"ORDER BY created_at DESC", (to_email,))
        row = cur.fetchone()
    if not row:
        return None
    return {"draft_id": row[0], "to_email": row[1], "subject": row[2], "body": row[3],
            "approved_at": row[4], "approved_by": row[5]}


def count_pending_drafts(to_email: str) -> int:
    """Сколько НЕотправленных черновиков висит на адрес (ревью R2: при нескольких без draft_id
    send_approved_email не должен молча взять новейший — апрув был на КОНКРЕТНЫЙ текст)."""
    to_email = str(to_email or "").strip().lower()
    with _conn() as c:
        cur = c.execute(f"SELECT COUNT(*) FROM email_drafts WHERE to_email={_PH} AND sent_at IS NULL",
                        (to_email,))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


def mark_draft_sent(draft_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"UPDATE email_drafts SET sent_at={_PH} WHERE draft_id={_PH}", (now, str(draft_id)))
        if not _IS_PG:
            c.commit()


def mark_draft_approved(draft_id: str, approver_id) -> bool:
    """Зафиксировать апрув Дмитрия на КОНКРЕТНЫЙ черновик (P1-гейт send_approved_email): True, если
    черновик найден и ещё НЕ отправлен. Ставит ТОЛЬКО код детекта апрува в поллере (по author_id
    сообщения), НЕ модель — в этом суть: «одобрено» становится машинным фактом, а не намерением LLM."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(f"UPDATE email_drafts SET approved_at={_PH}, approved_by={_PH} "
                        f"WHERE draft_id={_PH} AND sent_at IS NULL",
                        (now, str(approver_id), str(draft_id)))
        if not _IS_PG:
            c.commit()
        rc = getattr(cur, "rowcount", 0) or 0
    return rc > 0


def list_pending_drafts() -> "list[dict]":
    """Все НЕотправленные черновики (для корреляции апрува в поллере): [{draft_id, to_email,
    approved_at}]. Апрув в личке Дмитрия матчим на единственный неодобренный; в задаче — по email."""
    with _conn() as c:
        cur = c.execute("SELECT draft_id, to_email, approved_at FROM email_drafts "
                        "WHERE sent_at IS NULL")
        rows = cur.fetchall() or []
    return [{"draft_id": r[0], "to_email": r[1], "approved_at": r[2]} for r in rows]


def seen_message(message_id: str) -> bool:
    """True, если сообщение уже обработано (тогда пропускаем)."""
    with _conn() as c:
        cur = c.execute(f"SELECT 1 FROM processed_messages WHERE message_id={_PH}", (str(message_id),))
        return cur.fetchone() is not None


def mark_message(message_id: str, dialog_id: str) -> None:
    # ON CONFLICT DO NOTHING: при деплой-оверлапе Railway два воркера на одной БД могут оба пометить
    # одно сообщение — голый INSERT ронял второй тик UNIQUE-violation'ом. Идемпотентно; семантика
    # для Канта (возврат None) не меняется. message_id — PRIMARY KEY (см. init_db).
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO processed_messages VALUES ({_PH},{_PH},{_PH}) "
                  f"ON CONFLICT (message_id) DO NOTHING",
                  (str(message_id), str(dialog_id), now))
        if not _IS_PG:
            c.commit()


def add_spend(day: str, usd: float) -> None:
    """Накопить траты «мозга» за день (для суточного лимита DAILY_BUDGET_USD)."""
    if not usd:
        return
    with _conn() as c:
        c.execute(f"INSERT INTO brain_spend VALUES ({_PH},{_PH}) "
                  "ON CONFLICT(day) DO UPDATE SET usd = brain_spend.usd + excluded.usd",
                  (day, float(usd)))
        if not _IS_PG:
            c.commit()


def spend_today(day: str) -> float:
    with _conn() as c:
        cur = c.execute(f"SELECT usd FROM brain_spend WHERE day={_PH}", (day,))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] else 0.0


def budget_alerted(day: str) -> bool:
    with _conn() as c:
        cur = c.execute(f"SELECT 1 FROM budget_alert WHERE day={_PH}", (day,))
        return cur.fetchone() is not None


def mark_budget_alert(day: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO budget_alert VALUES ({_PH},{_PH})", (day, now))
        if not _IS_PG:
            c.commit()


def credit_alerted(day: str) -> bool:
    """Уже алертили Дмитрию про исчерпанный баланс Anthropic сегодня (дедуп — ошибка летит на каждом ходу)."""
    with _conn() as c:
        cur = c.execute(f"SELECT 1 FROM credit_alert WHERE day={_PH}", (day,))
        return cur.fetchone() is not None


def mark_credit_alert(day: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO credit_alert VALUES ({_PH},{_PH}) ON CONFLICT (day) DO NOTHING", (day, now))
        if not _IS_PG:
            c.commit()


def unmark_message(message_id: str) -> None:
    """Снять отметку «обработано» (отправка не удалась → пусть следующий тик повторит)."""
    with _conn() as c:
        c.execute(f"DELETE FROM processed_messages WHERE message_id={_PH}", (str(message_id),))
        if not _IS_PG:
            c.commit()


def pinged_recently(task_id: str, user_id: str, within_hours: int = 24) -> bool:
    """True, если боту уже пинговали эту задачу/юзера за последние N часов."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    with _conn() as c:
        cur = c.execute(
            f"SELECT 1 FROM ping_log WHERE task_id={_PH} AND user_id={_PH} AND sent_at>{_PH}",
            (str(task_id), str(user_id), cutoff))
        return cur.fetchone() is not None


def mark_ping(task_id: str, user_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(f"INSERT INTO ping_log VALUES ({_PH},{_PH},{_PH})",
                  (str(task_id), str(user_id), now))
        if not _IS_PG:
            c.commit()
