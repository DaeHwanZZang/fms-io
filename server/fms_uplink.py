"""
라즈베리파이 -> FMS 신호 업로드. 플래그가 바뀌면 바로, 안 바뀌어도 주기적으로
`PUT {FMS_URL}/api/v1/flags/{name}` 로 보낸다 (FMS 를 재시작해도 값이 복구된다).
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

import io_config

log = logging.getLogger("io.uplink")


class FmsUplink:
    def __init__(self, provider: Callable[[], dict], on_status: Callable[[], None]) -> None:
        self._provider = provider        # () -> {flag: bool}
        self._on_status = on_status      # 상태가 바뀌면 호출 (대시보드 갱신용)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._sent: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._status = {"url": io_config.FMS_URL, "ok": None, "error": None, "last_sent": None}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fms-uplink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def mark_dirty(self) -> None:
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _put(self, name: str, value: bool) -> None:
        req = urllib.request.Request(
            f"{io_config.FMS_URL}/api/v1/flags/{name}",
            data=json.dumps({"value": value}).encode(),
            headers={"content-type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=io_config.UPLINK_TIMEOUT_SEC) as r:
            r.read()

    def _run(self) -> None:
        last_full = 0.0
        while not self._stop.is_set():
            self._wake.wait(timeout=io_config.UPLINK_INTERVAL_SEC)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                last_full = self._round(last_full)
            except Exception:   # GPIO 읽기/브로드캐스트 오류로 업로드 스레드가 죽으면 FMS 로 신호가 영영 안 간다
                log.exception("FMS uplink 라운드 실패 (다음 주기에 재시도)")

    def _round(self, last_full: float) -> float:
        """한 번 업로드한다. 다음 라운드의 last_full 을 돌려준다."""
        flags = self._provider()
        full = time.monotonic() - last_full >= io_config.UPLINK_INTERVAL_SEC
        todo = {n: v for n, v in flags.items() if full or self._sent.get(n) != v}
        if not todo:
            return last_full
        ok, error = True, None
        for name, value in todo.items():
            try:
                self._put(name, value)
                self._sent[name] = value
            except (urllib.error.URLError, OSError, ValueError) as exc:
                ok, error = False, str(getattr(exc, "reason", exc))
                self._sent.pop(name, None)   # 실패한 값은 다음 라운드에 다시 보낸다
                break
        if full and ok:
            last_full = time.monotonic()
        with self._lock:
            changed = (self._status["ok"], self._status["error"]) != (ok, error)
            self._status.update(ok=ok, error=error)
            if ok:
                self._status["last_sent"] = time.time()
        if changed:
            log.info("FMS uplink %s", "OK" if ok else f"실패: {error}")
        self._on_status()
        return last_full
