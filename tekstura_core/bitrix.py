"""Тонкий клиент Bitrix24 REST поверх входящего вебхука бота 2206.

Подводные камни (см. волт _Обзор-автоматизаций):
- ошибки приходят с HTTP 200 + {"error": ...} — всегда проверяем;
- rate limit ~2 req/sec → один лимитер на процесс, шаг между СТАРТАМИ запросов
  `config.BITRIX_MIN_INTERVAL_SEC` (0.5 с по умолчанию), см. `_Limiter`.
"""
import re
import threading
import time
from contextlib import contextmanager

import httpx
from . import config


class _Limiter:
    """Один лимитер портала на процесс: слот — момент СТАРТА запроса, сон — вне лока.

    До v0.3.1 здесь был глобальный `_last_call` без лока, а пауза 0,4 с отсчитывалась от КОНЦА
    предыдущего ответа. В одном потоке это давало фактически ~1,1–1,4 запроса/с. С двумя
    потоками — гонка: оба видят «прошло ≥0,4 с» и стреляют разом. Офлайн-симуляция 26.09.2026
    (2 потока × 5 вызовов, задержка портала 0,5 с): 5 интервалов из 9 ≈ 0 с — темп до ~2×,
    портал отвечает QUERY_LIMIT_EXCEEDED, и ретраи съедают весь выигрыш второго потока.

    Почему от старта, а не от конца: лимит портала считает запросы, пришедшие в секунду, а не
    паузы между ответами. Интервал 0,5 с от старта — ровно 2 запроса/с при любой задержке сети.
    (0,4 с от старта дали бы до 2,5/с — выше лимита.)

    Приоритет. Поток, взявший `priority()`, получает освободившийся слот раньше обычных, даже
    если те ждали дольше: клик человека не должен стоять в общей очереди за свипом. Цена —
    обычные могут ждать, пока приоритетные идут подряд; поэтому приоритет берут только короткие
    обработчики. Лок листовой: под ним нет ни вызовов, ни сна — `Condition.wait` его отпускает.
    """

    def __init__(self, interval: float):
        self.interval = float(interval)
        self._cond = threading.Condition(threading.Lock())
        self._next = 0.0                    # monotonic: самый ранний старт следующего запроса
        self._waiting = {True: 0, False: 0}  # сколько ждут слота: приоритетные / обычные

    def acquire(self, high: bool = False) -> float:
        """Дождаться своего слота и занять его. Возврат — момент старта (monotonic)."""
        high = bool(high)
        with self._cond:
            self._waiting[high] += 1
            try:
                while True:
                    now = time.monotonic()
                    if now >= self._next and (high or not self._waiting[True]):
                        self._next = now + self.interval
                        return now
                    # Слот ещё не наступил — ждём ровно до него. Слот наступил, но его ждёт
                    # приоритетный — ждём, пока он уйдёт (он разбудит всех на выходе).
                    self._cond.wait(self._next - now if now < self._next else None)
            finally:
                self._waiting[high] -= 1
                self._cond.notify_all()


_LIMITER = _Limiter(config.BITRIX_MIN_INTERVAL_SEC)
_tls = threading.local()


@contextmanager
def priority():
    """Все вызовы портала внутри блока (в ЭТОМ потоке) идут приоритетной полосой лимитера."""
    prev = getattr(_tls, "high", False)
    _tls.high = True
    try:
        yield
    finally:
        _tls.high = prev


def throttle() -> float:
    """Занять слот лимитера портала, не делая запроса. Для тех, кто ходит в портал мимо
    `call_with` (скачивание файлов, свой httpx) — чтобы их запросы тоже считались."""
    return _LIMITER.acquire(getattr(_tls, "high", False))


def _safe_client_filename(filename: str, default: str = "Ponuda") -> str:
    """Имя файла для КЛИЕНТА мессенджера. Коннектор Wazzup→Viber обрезает имя по пробелу/спецсимволу: боевой
    кейс Bane #2214 — «PONUDA #2214_Kvarc.pdf» дошло как «PONUDA» БЕЗ «.pdf» → клиент не смог открыть (формат
    не определился). Чистим до [A-Za-z0-9._-] (пробел/«#» → «_»), гарантируем «.pdf»."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "").strip("_") or default
    return base if base.lower().endswith(".pdf") else f"{base}.pdf"


def call_with(base: str, method: str, params: dict | None = None) -> dict:
    """Вызвать REST под ПРОИЗВОЛЬНЫМ вебхуком (напр. хук Канта 2206 для КБ-группы — там Кант участник,
    а Milica нет). Бросает RuntimeError, если Bitrix вернул {"error"}."""
    throttle()                           # rate limit ~2 req/sec на процесс, см. _Limiter
    url = f"{base.rstrip('/')}/{method}.json"
    # JSON-тело: корректно кодирует вложенные структуры (filter/select/order),
    # form-data их ломает («Should be value of type array»).
    resp = httpx.post(url, json=params or {}, timeout=30)
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Bitrix {method}: {data.get('error')} {data.get('error_description','')}")
    return data


def call(method: str, params: dict | None = None) -> dict:
    """Вызвать метод REST под основным вебхуком процесса. Бросает RuntimeError при {"error"}."""
    return call_with(config.BITRIX_WEBHOOK_BASE, method, params)


def send_message(dialog_id: str, text: str) -> int:
    """Ответить в диалог от имени бота (imbot.message.add)."""
    r = call("imbot.message.add", {"BOT_ID": config.BITRIX_BOT_ID,
                                    "DIALOG_ID": dialog_id, "MESSAGE": text})
    return r.get("result")


def get_dialog_history(dialog_id: str, limit: int = 15) -> list[dict]:
    """История диалога (новые → старые). Используется для контекста и дедупа.

    К каждому сообщению добавляем list вложений `files` ({id,type,name}) — нужно для голосовых.
    """
    r = call("im.dialog.messages.get", {"DIALOG_ID": dialog_id, "LIMIT": limit})
    res = r.get("result", {})
    users = {u["id"]: u.get("name", "") for u in res.get("users", [])}

    # files может прийти как dict {id: meta} или как list [meta]
    files_raw = res.get("files", {}) or {}
    if isinstance(files_raw, list):
        files_idx = {str(f.get("id")): f for f in files_raw}
    else:
        files_idx = {str(k): v for k, v in files_raw.items()}

    out = []
    for m in res.get("messages", []):
        params = m.get("params") or {}
        fids = params.get("FILE_ID") or []
        mfiles = [files_idx[str(fid)] for fid in fids if str(fid) in files_idx]
        out.append({"id": m["id"], "author_id": int(m["author_id"]),
                    "author": users.get(m["author_id"], str(m["author_id"])),
                    "text": m.get("text", ""), "date": m.get("date"),
                    "files": mfiles})
    return out


def task_chat_messages(task_id, limit: int = 20) -> list[dict]:
    """Комментарии задачи (новые→старые). ВАЖНО: на этом портале task.commentitem.getlist отдаёт
    пустой [] — комментарии задач переехали в чат-движок (deprecated tasks 25.700.0). Реально читаются
    ТОЛЬКО через чат задачи: tasks.task.get → chatId → im.dialog.messages.get на chat<chatId>.
    Возвращает [{id, author_id, author, text, date, files}] (как get_dialog_history). [] если чата нет."""
    t = (call("tasks.task.get", {"taskId": int(task_id)}).get("result", {}) or {}).get("task", {})
    chat_id = t.get("chatId") or t.get("CHAT_ID")
    if not chat_id:
        return []
    return get_dialog_history(f"chat{chat_id}", limit=limit)


def send_image(dialog_id: str, url: str, caption: str = "") -> int:
    """Отправить картинку в чат. Внешние URL Битрикс не рендерит, поэтому скачиваем и заливаем
    в дисковую папку чата, затем шлём вложением IMAGE по внутренней ссылке."""
    import base64
    import json as _json
    data = httpx.get(url, timeout=30, follow_redirects=True).content
    fid = (call("im.disk.folder.get", {"DIALOG_ID": dialog_id}).get("result") or {}).get("ID")
    safe = "".join(c for c in (caption or "photo") if c.isalnum() or c in " _-")[:40].strip() or "photo"
    up = (call("disk.folder.uploadfile", {
        "id": fid, "data": {"NAME": f"{safe}.jpg"},
        "fileContent": base64.b64encode(data).decode(), "generateUniqueName": "Y",
    }).get("result") or {})
    link = up.get("DOWNLOAD_URL")
    attach = _json.dumps([{"IMAGE": {"NAME": caption or "фото", "LINK": link}}])
    r = call("im.message.add", {"DIALOG_ID": dialog_id, "MESSAGE": caption, "ATTACH": attach})
    return r.get("result")


def send_file_to_client(dialog_id: str, disk_file_id, filename: str, caption: str = "") -> dict:
    """Отправить КЛИЕНТУ открытой линии РЕАЛЬНЫЙ файл (PDF-КП), а не ссылку. Ключ — `im.disk.file.commit`:
    создаёт сообщение с `params.FILE_ID` (тот же тип, что ВХОДЯЩИЕ файлы клиента Viber→Bitrix), поэтому
    коннектор Wazzup сам качает байты и шлёт файлом в Viber. ATTACH-вложение и публичная /doc/-ссылка для
    клиента не работают (ссылка требует логина в портал). Возврат {ok, message_id, reason}.
    dialog_id='chatXXXX'; disk_file_id=OBJECT_ID PDF на диске (как из kp_delivery._result_files)."""
    import base64
    chat_num = dialog_id[4:] if dialog_id.startswith("chat") else str(dialog_id)   # commit хочет ГОЛЫЙ номер
    try:
        call("imopenlines.session.join", {"CHAT_ID": chat_num})                   # бот должен быть в сессии ОЛ
    except Exception:  # noqa: BLE001
        pass
    try:
        _nm, data = download_file(disk_file_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message_id": None, "reason": f"download: {e}"}
    if not data:
        return {"ok": False, "message_id": None, "reason": "empty_disk_file"}
    folder = (call("im.disk.folder.get", {"DIALOG_ID": dialog_id}).get("result") or {}).get("ID")
    safe = _safe_client_filename(filename)   # пробел/«#» в имени → Wazzup/Viber теряет «.pdf» (кейс Bane #2214)
    up = (call("disk.folder.uploadfile", {
        "id": folder, "data": {"NAME": safe},
        "fileContent": base64.b64encode(data).decode(), "generateUniqueName": "Y",
    }).get("result") or {})
    chat_disk_id = up.get("ID")
    if not chat_disk_id:
        return {"ok": False, "message_id": None, "reason": "upload_failed"}
    commit = call("im.disk.file.commit", {
        "CHAT_ID": chat_num, "DISK_ID": chat_disk_id, "MESSAGE": caption or "",
    }).get("result") or {}
    msg_id = commit.get("MESSAGE_ID") or commit.get("ID")
    if not msg_id:
        return {"ok": False, "message_id": None, "reason": "commit_no_msg"}
    has_file = False                                                              # подтверждение: файл реально в сообщении
    try:
        for m in get_dialog_history(dialog_id, limit=5):
            if str(m.get("id")) == str(msg_id) and m.get("files"):
                has_file = True
                break
    except Exception:  # noqa: BLE001
        pass
    if not has_file:
        print(f"[bitrix] send_file_to_client: msg {msg_id} без подтверждённого files[] (commit отработал)", flush=True)
    return {"ok": True, "message_id": msg_id, "reason": "ok"}


def send_image_to_client(dialog_id: str, image_url: str, caption: str = "", filename: str = "") -> dict:
    """Отправить КЛИЕНТУ открытой линии ФОТО товара по ВНЕШНЕМУ URL (каталог поставщика) так, чтобы оно
    ДОШЛО в Viber/Wazzup. Как send_file_to_client, но источник — URL (скачиваем сами) + по возможности
    конверт WebP→JPEG (Viber дружелюбнее к jpg; Kobex отдаёт webp). Ключ — `im.disk.file.commit`
    (params.FILE_ID, тот же тип, что входящие файлы клиента), а НЕ `ATTACH IMAGE`: ATTACH коннектор Wazzup
    НЕ форвардит наружу (боевой кейс Ana 16.06 — доходила только подпись, фото нет). Поставщик не палится:
    клиент видит файл с диска Bitrix, не ссылку kobex. Возврат {ok, message_id, reason}."""
    import base64
    chat_num = dialog_id[4:] if dialog_id.startswith("chat") else str(dialog_id)
    try:
        data = httpx.get(image_url, timeout=30, follow_redirects=True).content
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message_id": None, "reason": f"download: {e}"}
    if not data:
        return {"ok": False, "message_id": None, "reason": "empty_image"}
    ext = "jpg"
    try:                                            # WebP→JPEG, если Pillow есть; иначе шлём оригинал
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        data = buf.getvalue()
    except Exception:  # noqa: BLE001 — нет Pillow / не картинка → отправим как есть, тип по URL
        low = image_url.lower()
        ext = "webp" if ".webp" in low else ("png" if ".png" in low else "jpg")
    base = "".join(c for c in (filename or "foto") if c.isalnum() or c in " _-")[:40].strip() or "foto"
    try:
        call("imopenlines.session.join", {"CHAT_ID": chat_num})                   # бот должен быть в сессии ОЛ
    except Exception:  # noqa: BLE001
        pass
    folder = (call("im.disk.folder.get", {"DIALOG_ID": dialog_id}).get("result") or {}).get("ID")
    if not folder:
        return {"ok": False, "message_id": None, "reason": "no_chat_folder"}
    up = (call("disk.folder.uploadfile", {
        "id": folder, "data": {"NAME": f"{base}.{ext}"},
        "fileContent": base64.b64encode(data).decode(), "generateUniqueName": "Y",
    }).get("result") or {})
    chat_disk_id = up.get("ID")
    if not chat_disk_id:
        return {"ok": False, "message_id": None, "reason": "upload_failed"}
    commit = call("im.disk.file.commit", {
        "CHAT_ID": chat_num, "DISK_ID": chat_disk_id, "MESSAGE": caption or "",
    }).get("result") or {}
    msg_id = commit.get("MESSAGE_ID") or commit.get("ID")
    if not msg_id:
        return {"ok": False, "message_id": None, "reason": "commit_no_msg"}
    return {"ok": True, "message_id": msg_id, "reason": "ok"}


def send_image_bytes_to_client(dialog_id: str, data: bytes, caption: str = "",
                               filename: str = "image", ext: str = "png") -> dict:
    """Отправить КЛИЕНТУ открытой линии ГОТОВЫЕ БАЙТЫ картинки (напр. отрендеренную схему кухни PNG).
    Ядро как у send_image_to_client, но без скачивания URL: грузим байты на диск чата и коммитим через
    `im.disk.file.commit` (доходит по Wazzup/Viber, поставщик не палится). Возврат {ok, message_id, reason}."""
    import base64
    if not data:
        return {"ok": False, "message_id": None, "reason": "empty_image"}
    chat_num = dialog_id[4:] if dialog_id.startswith("chat") else str(dialog_id)
    base = "".join(c for c in (filename or "image") if c.isalnum() or c in " _-")[:40].strip() or "image"
    try:
        call("imopenlines.session.join", {"CHAT_ID": chat_num})
    except Exception:  # noqa: BLE001
        pass
    folder = (call("im.disk.folder.get", {"DIALOG_ID": dialog_id}).get("result") or {}).get("ID")
    if not folder:
        return {"ok": False, "message_id": None, "reason": "no_chat_folder"}
    up = (call("disk.folder.uploadfile", {
        "id": folder, "data": {"NAME": f"{base}.{ext}"},
        "fileContent": base64.b64encode(data).decode(), "generateUniqueName": "Y",
    }).get("result") or {})
    chat_disk_id = up.get("ID")
    if not chat_disk_id:
        return {"ok": False, "message_id": None, "reason": "upload_failed"}
    commit = call("im.disk.file.commit", {
        "CHAT_ID": chat_num, "DISK_ID": chat_disk_id, "MESSAGE": caption or "",
    }).get("result") or {}
    msg_id = commit.get("MESSAGE_ID") or commit.get("ID")
    if not msg_id:
        return {"ok": False, "message_id": None, "reason": "commit_no_msg"}
    return {"ok": True, "message_id": msg_id, "reason": "ok"}


def upload_bytes_to_disk(folder_id, name: str, data: bytes) -> "int | None":
    """Загрузить ГОТОВЫЕ байты файла в папку Bitrix-Диска → вернуть disk-file-id (для прикрепления к
    задаче через UF_TASK_WEBDAV_FILES как 'n<id>'). Используется генератором КП конфигуратора.
    None при сбое (не должен ронять постановку задачи)."""
    import base64
    if not (folder_id and data):
        return None
    safe = "".join(c for c in (name or "file") if c.isalnum() or c in " ._-").strip()[:80] or "file"
    try:
        up = (call("disk.folder.uploadfile", {
            "id": int(folder_id), "data": {"NAME": safe},
            "fileContent": base64.b64encode(data).decode(), "generateUniqueName": "Y",
        }).get("result") or {})
        fid = up.get("ID")
        return int(fid) if fid else None
    except Exception as e:  # noqa: BLE001
        print(f"[bitrix] upload_bytes_to_disk: {e}", flush=True)
        return None


def download_file(file_id) -> tuple[str, bytes]:
    """Скачать файл с Bitrix Disk по id. Возвращает (имя, байты)."""
    r = call("disk.file.get", {"id": file_id})
    res = r.get("result", {}) or {}
    name = res.get("NAME", f"file_{file_id}")
    url = res.get("DOWNLOAD_URL")
    if not url:
        return name, b""
    throttle()          # скачивание — тоже запрос к порталу, считается в общий лимит
    resp = httpx.get(url, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    return name, resp.content
