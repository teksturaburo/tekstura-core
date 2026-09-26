"""Общий конфиг ИИ-агентов TeksturaBuro — ТОЛЬКО универсальные env-значения.

Агент-специфика (база знаний, ночные свипы, свап ролей, бюджеты, вайтлисты) живёт
в `config_<agent>.py` каждого агента, НЕ здесь. Сюда попадает только то, что реально
общее для ядро-модулей (bitrix/dedup/dept_status/stt) и всех агентов.

Историческая заметка: раньше это был Кант-конфиг (`kant-server/app/config.py`), который
все импортировали по инерции. При извлечении в tekstura-core оставлен только универсальный
срез; дефолты сделаны нейтральными (напр. BITRIX_BOT_ID=0, а не 2206 Канта).
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ── Bitrix ────────────────────────────────────────────────────────────────
# Базовый URL входящего вебхука бота (rest/<id>/<token>/). Обязателен — падаем на импорте,
# если не задан (лучше явный отказ на старте, чем работа без Bitrix).
BITRIX_WEBHOOK_BASE = os.environ["BITRIX_WEBHOOK_BASE"].rstrip("/")
# ID бот-аккаунта агента. Дефолт 0 (нейтральный) — каждый агент задаёт свой через env.
BITRIX_BOT_ID = int(os.getenv("BITRIX_BOT_ID", "0"))
# Шаг между СТАРТАМИ запросов процесса к порталу (лимит ~2 запроса/с). До v0.3.1 была пауза
# 0,4 с от КОНЦА ответа без лока — см. `bitrix._Limiter`.
# Пол — 0,5 с, то есть не быстрее 2 запросов/с: env может шаг только УВЕЛИЧИТЬ (запас под соседей
# по порталу). Первая версия v0.3.1 держала пол 0,05 с, и значение из тестов (0,1), скопированное
# в прод, разогнало бы процесс до 10 запросов/с (ревью Codex 26.09.2026). Тестам быстрый шаг
# ставится отдельным явным вызовом `bitrix.set_min_interval_for_tests`, не через этот env.
BITRIX_MIN_INTERVAL_FLOOR_SEC = 0.5
BITRIX_MIN_INTERVAL_SEC = max(BITRIX_MIN_INTERVAL_FLOOR_SEC,
                              float(os.getenv("BITRIX_MIN_INTERVAL_SEC", "0.5")))

# ── Anthropic ───────────────────────────────────────────────────────────────
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
# Ключ (свой на агента, из env). Экспонирован в ядро для агентов, читающих config.ANTHROPIC_API_KEY
# (Milica и generic anthropic_client); агенты со своим config_<agent> могут читать и оттуда.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "1024"))
# Лимиты хода мозга (универсальные предохранители от runaway/дорогого цикла).
BRAIN_MAX_TURNS = int(os.getenv("BRAIN_MAX_TURNS", "6"))
BRAIN_MAX_BUDGET_USD = float(os.getenv("BRAIN_MAX_BUDGET_USD", "1.5"))

# ── Google Gemini (расшифровка голосовых в stt.py, эмбеддинги) ────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── БД (dedup / dept_status): Postgres на Railway, sqlite локально ────────────
DATABASE_URL = os.getenv("DATABASE_URL", "") or "sqlite:///agent.db"
# Таймауты Postgres (v0.3.1). До них соединение ядра не имело ни таймаута запроса, ни keepalive:
# зависший Postgres (или оборванная сеть без RST) держал вызывающий поток бесконечно. Для агента
# с потоком-слушателем (Ivana, 26.09.2026) это значит «поток держит единственного исполнителя
# кликов, кнопки не разбирает никто». Таймаут запроса — на сервере (statement_timeout, включает
# ожидание блокировок), обрыв сети ловят keepalive и tcp_user_timeout. 0 в
# DB_STATEMENT_TIMEOUT_MS снимает лимит запроса (не рекомендуется).
DB_CONNECT_TIMEOUT_SEC = max(1, int(os.getenv("DB_CONNECT_TIMEOUT_SEC", "10")))
DB_STATEMENT_TIMEOUT_MS = max(0, int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000")))


def pg_connect_kwargs() -> dict:
    """Параметры `psycopg.connect` для всех соединений ядра: таймауты и keepalive.

    keepalive: простой 30 с → пробы раз в 10 с → 3 пробы, то есть мёртвый сервер виден за ~60 с,
    а не за системные 2 часа. tcp_user_timeout 60 с — предел для неподтверждённых данных (Linux).
    """
    kw = {"connect_timeout": DB_CONNECT_TIMEOUT_SEC, "keepalives": 1, "keepalives_idle": 30,
          "keepalives_interval": 10, "keepalives_count": 3, "tcp_user_timeout": 60000}
    if DB_STATEMENT_TIMEOUT_MS:
        kw["options"] = f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"
    return kw

# ── Каналы / операционка ─────────────────────────────────────────────────────
DMITRY_USER_ID = int(os.getenv("DMITRY_USER_ID", "1"))
# Диалог эскалации к Дмитрию (строка — id личного чата). Дефолт "1".
ESCALATION_DIALOG_ID = os.getenv("ESCALATION_DIALOG_ID", "1")
# Бот «Bitrix24 Support» (техподдержка портала).
SUPPORT_USER_ID = int(os.getenv("SUPPORT_USER_ID", "4"))
# Базовый интервал опроса (агенты обычно переопределяют своим <AGENT>_POLL_INTERVAL_SEC).
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "15"))
TZ_NAME = os.getenv("TZ_NAME", "Europe/Belgrade")
