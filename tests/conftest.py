"""Окружение тестов ядра: без сети и без настоящих ключей.

Интервал лимитера в тестах 0,1 с (а не боевые 0,5), чтобы прогон занимал секунды.
Переменные ставятся ДО импорта ядра: config читает окружение на импорте.
"""
import os

os.environ.setdefault("BITRIX_WEBHOOK_BASE", "https://example.invalid/rest/0/test")
os.environ["BITRIX_MIN_INTERVAL_SEC"] = "0.1"
os.environ["DATABASE_URL"] = "sqlite:////tmp/tekstura-core-test-bootstrap.sqlite3"

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False
