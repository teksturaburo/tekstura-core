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

# ── Anthropic ───────────────────────────────────────────────────────────────
# Только модель по умолчанию; сам ключ агенты читают из env напрямую (свой ключ на агента).
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ── Google Gemini (расшифровка голосовых в stt.py, эмбеддинги) ────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── БД (dedup / dept_status): Postgres на Railway, sqlite локально ────────────
DATABASE_URL = os.getenv("DATABASE_URL", "") or "sqlite:///agent.db"

# ── Каналы / операционка ─────────────────────────────────────────────────────
DMITRY_USER_ID = int(os.getenv("DMITRY_USER_ID", "1"))
# Диалог эскалации к Дмитрию (строка — id личного чата). Дефолт "1".
ESCALATION_DIALOG_ID = os.getenv("ESCALATION_DIALOG_ID", "1")
# Бот «Bitrix24 Support» (техподдержка портала).
SUPPORT_USER_ID = int(os.getenv("SUPPORT_USER_ID", "4"))
# Базовый интервал опроса (агенты обычно переопределяют своим <AGENT>_POLL_INTERVAL_SEC).
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "15"))
TZ_NAME = os.getenv("TZ_NAME", "Europe/Belgrade")
