"""Лимитер портала: один на процесс, шаг между СТАРТАМИ, сон вне лока, приоритетная полоса.

До v0.3.1 `_last_call` был глобальной переменной без лока, а пауза 0,4 с считалась от КОНЦА
ответа. Офлайн-симуляция 26.09.2026 (2 потока × 5 вызовов): 5 интервалов из 9 ≈ 0 с. Тест ниже
написан так, чтобы на v0.2.0 падать: интервал он берёт из config, а если поля нет — 0,4 с.
"""
import threading
import time

import pytest

from tekstura_core import bitrix, config

#: Шаг тестового лимитера (conftest ставит его явным `set_min_interval_for_tests`). Боевой шаг
#: из config здесь не берём: он не ниже 0,5 с, и прогон растянулся бы впятеро.
INTERVAL = 0.1
EPS = 0.01          # запас на гранулярность часов, а не на поведение


class _Resp:
    def json(self):
        return {"result": True}


@pytest.fixture
def portal(monkeypatch):
    """Фейковый портал: пишет момент СТАРТА каждого запроса, отвечает с задержкой."""
    st = {"starts": [], "latency": 0.05, "lock_free_during_request": []}
    guard = threading.Lock()

    def post(url, json=None, timeout=None):
        with guard:
            st["starts"].append(time.monotonic())
        limiter = getattr(bitrix, "_LIMITER", None)
        if limiter is not None:
            got = limiter._cond.acquire(blocking=False)
            st["lock_free_during_request"].append(got)
            if got:
                limiter._cond.release()
        time.sleep(st["latency"])
        return _Resp()

    monkeypatch.setattr(bitrix.httpx, "post", post)
    if hasattr(bitrix, "_Limiter"):
        monkeypatch.setattr(bitrix, "_LIMITER", bitrix._Limiter(INTERVAL))
    return st


def _gaps(starts):
    s = sorted(starts)
    return [b - a for a, b in zip(s, s[1:])]


def test_four_threads_never_start_closer_than_the_interval(portal):
    """Главная гарантия: сколько бы потоков ни звали портал, старты разнесены на интервал."""
    def worker():
        for _ in range(5):
            bitrix.call_with("https://example.invalid/rest/0/test", "crm.deal.get", {})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(portal["starts"]) == 20
    gaps = _gaps(portal["starts"])
    assert min(gaps) >= INTERVAL - EPS, f"старты ближе интервала: {min(gaps):.3f} < {INTERVAL}"


def test_interval_counts_from_start_not_from_end(portal):
    """Медленный ответ не растягивает шаг: следующий старт — через интервал от ПРОШЛОГО старта.

    Пауза «от конца ответа» при задержке портала 0,3 с давала бы шаг 0,4 с; здесь один поток,
    так что шаг равен max(интервал, задержка), и он не больше интервала + задержки.
    """
    portal["latency"] = INTERVAL * 3
    for _ in range(3):
        bitrix.call_with("https://example.invalid/rest/0/test", "crm.deal.get", {})
    gaps = _gaps(portal["starts"])
    assert all(g < INTERVAL * 3 + INTERVAL * 0.9 for g in gaps), gaps


def test_request_is_made_outside_the_lock(portal):
    """Под локом лимитера нет ни вызовов, ни сна: иначе второй поток ждал бы чужой ответ."""
    bitrix.call_with("https://example.invalid/rest/0/test", "crm.deal.get", {})
    bitrix.call_with("https://example.invalid/rest/0/test", "crm.deal.get", {})
    assert portal["lock_free_during_request"] == [True, True]


def test_waiting_high_priority_gets_the_next_slot_first(portal):
    """Клик человека не стоит в общей очереди за свипом: приоритетный обгоняет ждущих обычных.

    Четыре раунда по четыре обычных: без приоритета приоритетный первым оказывается случайно
    (замер на мутанте: 1 раз из 10), и четыре раунда подряд — с вероятностью ~0,2 %.
    """
    positions = []
    for _ in range(4):
        limiter = bitrix._LIMITER = bitrix._Limiter(0.2)
        order, lock = [], threading.Lock()
        limiter.acquire()                        # слот занят — следующий через 0,2 с

        def normal(i):
            limiter.acquire(False)
            with lock:
                order.append(f"n{i}")

        def high():
            with bitrix.priority():
                bitrix.throttle()
            with lock:
                order.append("high")

        normals = [threading.Thread(target=normal, args=(i,)) for i in range(4)]
        for t in normals:
            t.start()
        time.sleep(0.05)                         # обычные уже ждут
        h = threading.Thread(target=high)
        h.start()
        for t in normals + [h]:
            t.join(timeout=10)
        positions.append(order.index("high"))
    assert positions == [0, 0, 0, 0], positions


def test_priority_is_per_thread_and_restored():
    assert not getattr(bitrix._tls, "high", False)
    seen = {}

    def other():
        seen["other"] = getattr(bitrix._tls, "high", False)

    with bitrix.priority():
        t = threading.Thread(target=other)
        t.start()
        t.join()
        with bitrix.priority():
            pass
        seen["inner_after"] = bitrix._tls.high
    assert seen == {"other": False, "inner_after": True}
    assert bitrix._tls.high is False


def test_throttle_shares_the_budget_with_calls(portal):
    """Запросы мимо call_with (скачивание файлов) считаются в тот же лимит."""
    marks = []
    bitrix.call_with("https://example.invalid/rest/0/test", "crm.deal.get", {})
    marks.append(bitrix.throttle())
    bitrix.call_with("https://example.invalid/rest/0/test", "crm.deal.get", {})
    all_starts = sorted(portal["starts"] + marks)
    assert min(_gaps(all_starts)) >= INTERVAL - EPS


def test_env_cannot_push_the_interval_below_the_portal_floor(monkeypatch):
    """Пол 0,5 с держится на любом значении env: 0, тестовые 0,1, отрицательное."""
    import importlib
    try:
        for raw in ("0", "0.1", "0.49", "-1"):
            monkeypatch.setenv("BITRIX_MIN_INTERVAL_SEC", raw)
            fresh = importlib.reload(config)
            assert fresh.BITRIX_MIN_INTERVAL_SEC >= 0.5, raw
        monkeypatch.setenv("BITRIX_MIN_INTERVAL_SEC", "0.8")
        assert importlib.reload(config).BITRIX_MIN_INTERVAL_SEC == 0.8   # вверх — можно
    finally:
        monkeypatch.delenv("BITRIX_MIN_INTERVAL_SEC", raising=False)
        importlib.reload(config)


_PROD_PROBE = r"""
import json, os, time
os.environ["BITRIX_MIN_INTERVAL_SEC"] = "0.1"      # значение из тестов, скопированное в прод
from tekstura_core import bitrix
starts = []

class R:
    def json(self):
        return {"result": True}

def post(url, json=None, timeout=None):
    starts.append(time.monotonic())
    return R()

bitrix.httpx.post = post
for _ in range(5):
    bitrix.call("crm.deal.get", {})
print(json.dumps({"interval": bitrix._LIMITER.interval, "starts": starts}))
"""


def test_fresh_process_is_never_faster_than_two_requests_a_second():
    """Боевой путь целиком: свежий процесс, env 0,1 — а портал всё равно получает ≤ 2 запроса/с.

    Отдельный процесс, потому что лимитер собирается на импорте, а в этом процессе conftest уже
    поставил тестовый шаг.
    """
    import json
    import os
    import subprocess
    import sys
    env = dict(os.environ, BITRIX_WEBHOOK_BASE="https://example.invalid/rest/0/test",
               DATABASE_URL="sqlite:////tmp/tekstura-core-test-bootstrap.sqlite3")
    env.pop("BITRIX_MIN_INTERVAL_SEC", None)
    out = subprocess.run([sys.executable, "-c", _PROD_PROBE], env=env, capture_output=True,
                         text=True, timeout=60, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["interval"] >= 0.5
    starts = got["starts"]
    assert len(starts) == 5
    assert min(_gaps(starts)) >= 0.5 - EPS
    rate = (len(starts) - 1) / (starts[-1] - starts[0])
    assert rate <= 2.0 + 0.05, f"{rate:.2f} запроса/с"


def test_test_hook_is_the_only_way_below_the_floor():
    """Быстрый шаг тестов ставится явным вызовом и именно в лимитер, а не в config."""
    assert bitrix._LIMITER.interval < 0.5            # conftest: set_min_interval_for_tests(0.1)
    assert config.BITRIX_MIN_INTERVAL_SEC >= 0.5     # config при этом боевой
