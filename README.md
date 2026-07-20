# tekstura-core

Общее ядро ИИ-агентов TeksturaBuro. Извлечено из монорепо `kant-server` (split-миграция, пилот Nikola 2026-07).

## Модули

| Модуль | Что |
|---|---|
| `config` | Универсальные env-значения (Bitrix/Anthropic-модель/Gemini/БД/TZ). Агент-специфика — в `config_<agent>.py` агента, НЕ здесь. |
| `bitrix` | Клиент Bitrix24 REST (входящий вебхук бота). |
| `dedup` | Дедуп сообщений + вспом. таблицы состояния (Postgres/sqlite по `DATABASE_URL`). |
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
