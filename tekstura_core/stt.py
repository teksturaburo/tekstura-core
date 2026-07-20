"""Расшифровка речи (голосовые сообщения) через Google Gemini.

Claude аудио не принимает, поэтому голосовое сначала переводим в текст здесь, а текст уже идёт в
anthropic_client как обычное сообщение. Gemini Flash принимает аудио инлайном (base64).
"""
import base64
import httpx
from . import config

_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# m4a из Битрикса распознаётся как audio/aac (проверено); прочие — по расширению
_MIME = {
    "m4a": "audio/aac", "aac": "audio/aac", "mp3": "audio/mp3", "mpeg": "audio/mp3",
    "ogg": "audio/ogg", "oga": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
    "aiff": "audio/aiff",
}

_PROMPT = ("Расшифруй это голосовое сообщение дословно. "
           "Верни только текст расшифровки, без комментариев и пояснений.")


def _mime_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MIME.get(ext, "audio/aac")


def transcribe(audio_bytes: bytes, filename: str) -> str:
    """Расшифровать аудио в текст. Пустая строка, если ключ не задан или ошибка."""
    if not config.GEMINI_API_KEY or not audio_bytes:
        return ""
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": _mime_for(filename),
                         "data": base64.b64encode(audio_bytes).decode()}},
        {"text": _PROMPT},
    ]}]}
    url = _URL.format(model=config.GEMINI_MODEL)
    for _ in range(3):  # 503 = модель временно перегружена → повтор
        r = httpx.post(url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=120)
        if r.status_code == 503:
            continue
        r.raise_for_status()
        cands = r.json().get("candidates", [])
        if not cands:
            return ""
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()
    return ""
