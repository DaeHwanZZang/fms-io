"""
외부 플래그 저장소 ([homework_iot 추가])
=======================================

현장 신호(라즈베리파이 I/O 등)를 FMS 가 job 의 `wait_flag` 명령에서 읽는 곳.
라즈베리파이 쪽이 `PUT /api/v1/flags/{name}` 으로 값을 올리고 내린다 (api/flags.py).

    True  = 올라가 있음 (신호 활성)      -> wait_flag 는 계속 대기
    False = 내려감                       -> wait_flag 통과
    None  = 한 번도 못 받음(unknown)     -> 계속 대기 (신호원이 죽었는데 그냥 지나가면 위험하다)
    만료  = 마지막 갱신이 task.FLAG_STALE_SEC 보다 오래됨 -> unknown 과 같이 취급 (파이가 끊긴 경우)

메모리에만 둔다. 서버를 재시작하면 신호원이 다시 보내야 한다.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class FlagError(Exception):
    pass


def check_flag_name(name: str) -> None:
    if not name or len(name) > 64 or not all(c.isalnum() or c in "_-." for c in name):
        raise FlagError(f"허용되지 않는 플래그 이름: {name!r} (영숫자/_/-/. 64자 이하)")


class FlagStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flags: dict[str, tuple[bool, float]] = {}   # name -> (값, 마지막 갱신 시각)

    def set(self, name: str, value: bool) -> None:
        check_flag_name(name)
        with self._lock:
            self._flags[name] = (bool(value), time.time())

    def get(self, name: str) -> Optional[bool]:
        with self._lock:
            entry = self._flags.get(name)
        return None if entry is None else entry[0]

    def entry(self, name: str) -> Optional[tuple[bool, float]]:
        """(값, 마지막 갱신 시각) 또는 None. 값이 오래됐는지(만료)는 호출한 쪽이 판단한다."""
        with self._lock:
            return self._flags.get(name)

    def snapshot(self) -> dict[str, dict]:
        now = time.time()
        with self._lock:
            return {n: {"value": v, "updated_at": ts, "age": round(now - ts, 2)}
                    for n, (v, ts) in sorted(self._flags.items())}
