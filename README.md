# tekstura-core

Общее ядро ИИ-агентов TeksturaBuro. Извлечено из монорепо `kant-server` (split-миграция, пилот Nikola 2026-07).

## Модули

| Модуль | Что |
|---|---|
| `config` | Универсальные env-значения (Bitrix/Anthropic-модель/Gemini/БД/TZ). Агент-специфика — в `config_<agent>.py` агента, НЕ здесь. |
| `bitrix` | Клиент Bitrix24 REST (входящий вебхук бота). С v0.3.1 — один потокобезопасный лимитер на процесс: шаг между СТАРТАМИ запросов `BITRIX_MIN_INTERVAL_SEC` (0,5 с; env может его только увеличить — пол 0,5 с, то есть не быстрее 2 запросов/с; тестам — явный `bitrix.set_min_interval_for_tests`), `bitrix.priority()` — приоритетная полоса потока, `bitrix.throttle()` — слот для запросов мимо `call_with`. |
| `dedup` | Дедуп сообщений + вспом. таблицы состояния (Postgres/sqlite по `DATABASE_URL`). С v0.3.1 соединения Postgres (и в `dept_status`) идут с таймаутами: `DB_CONNECT_TIMEOUT_SEC` (10), `DB_STATEMENT_TIMEOUT_MS` (30 000), keepalive — `config.pg_connect_kwargs()`. |
| `dept_status` | Клиент штаб-БД отдела (`DEPT_STATUS_DATABASE_URL` → общий Postgres). |
| `stt` | Расшифровка голосовых через Gemini. |

## Подключение из агент-репо

```
# requirements.txt
tekstura-core @ git+https://github.com/teksturaburo/tekstura-core@v0.1.0
```

Импорты в коде агента: `from tekstura_core import bitrix, config, dedup, dept_status, stt`.

**Приватный репо** → на билде Railway нужен read-only PAT как build-переменная:
`git+https://x-access-token:${GH_PAT}@github.com/teksturaburo/tekstura-core@v0.1.0`.

## Версии

semver-теги. Ритуал: правка ядра → тег `vX.Y.Z` → бамп пина в агент-репо → push (деплоит только этого агента). Изоляция = фича: плохой релиз ядра доедет до агента только при бампе пина.

Тесты ядра: `python3 -m pytest -q tests` (лимитер портала и атомарный `bump_attempt`).

**Перед тегом — ещё и на настоящем Postgres** (с v0.3.2): `TEST_PG_URL=postgresql://…/<пустая БД> python3 -m pytest -q tests`. Без `TEST_PG_URL` PG-тесты пропускаются (`skipped`), а на SQLite не видно
целого класса дефектов: `REAL` там 8 байт, на Postgres — float4 (v0.3.2), голый `%` в SQL с параметрами
psycopg принимает за плейсхолдер. Локальный Postgres без Docker: `pip install pgserver` (Python ≤ 3.12).
