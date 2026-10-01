"""
가상 GPIO — RPi.GPIO 와 같은 이름·시그니처. mock 전용 메서드는 `set_input` 뿐.

    try:
        import RPi.GPIO as GPIO
    except (ImportError, RuntimeError):
        from mockgpio import GPIO

이 모듈의 레벨은 전기 레벨(HIGH=1/LOW=0)이다. 액티브 로우 같은 논리 해석은 모른다.
로그는 `[MOCK]` 접두사.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("mockgpio")


class _MockGPIO:
    # 상수 값은 RPi.GPIO 와 같다 (숫자로 비교/저장하는 코드가 실물에서도 그대로 동작하도록)
    BCM = 11
    BOARD = 10
    OUT = 0
    IN = 1
    LOW = 0
    HIGH = 1
    PUD_OFF = 20
    PUD_DOWN = 21
    PUD_UP = 22
    RISING = 31
    FALLING = 32
    BOTH = 33

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode = None
        self._dir: dict[int, str] = {}
        self._level: dict[int, int] = {}
        self._pud: dict[int, str] = {}
        self._events: dict[int, tuple[int, object, float]] = {}   # pin -> (edge, callback, bouncetime s)
        self._last_cb: dict[int, float] = {}

    # -- RPi.GPIO 호환 ---------------------------------------------------

    def setwarnings(self, flag: bool) -> None:
        pass

    def setmode(self, mode) -> None:
        self._mode = mode
        log.info("[MOCK] setmode(%s)", mode)

    def setup(self, pin, direction, pull_up_down=None, initial=None) -> None:
        with self._lock:
            self._dir[pin] = direction
            if direction == self.OUT:
                self._level[pin] = self.LOW if initial is None else int(initial)
            else:
                pud = pull_up_down or self.PUD_OFF
                self._pud[pin] = pud
                # 입력 핀의 기본 레벨 = 풀업이면 HIGH, 아니면 LOW
                self._level[pin] = self.HIGH if pud == self.PUD_UP else self.LOW
        log.info("[MOCK] setup(BCM%s, %s, pud=%s, initial=%s)", pin, direction, pull_up_down, initial)

    def output(self, pin, value) -> None:
        with self._lock:
            if self._dir.get(pin) != self.OUT:
                raise RuntimeError(f"BCM{pin} 는 출력으로 setup 되지 않았다")
            self._level[pin] = int(bool(value))
        log.info("[MOCK] output(BCM%s, %s)", pin, int(bool(value)))

    def input(self, pin) -> int:
        with self._lock:
            if pin not in self._dir:
                raise RuntimeError(f"BCM{pin} 는 setup 되지 않았다")
            return self._level[pin]

    def add_event_detect(self, pin, edge, callback=None, bouncetime=0) -> None:
        with self._lock:
            self._events[pin] = (edge, callback, bouncetime / 1000.0)
        log.info("[MOCK] add_event_detect(BCM%s, %s)", pin, edge)

    def remove_event_detect(self, pin) -> None:
        with self._lock:
            self._events.pop(pin, None)

    def cleanup(self, pin=None) -> None:
        with self._lock:
            pins = [pin] if pin is not None else list(self._dir)
            for p in pins:
                for table in (self._dir, self._level, self._pud, self._events, self._last_cb):
                    table.pop(p, None)
        log.info("[MOCK] cleanup(%s)", pin if pin is not None else "all")

    # -- mock 전용 -------------------------------------------------------

    def set_input(self, pin, level) -> None:
        """외부 세계가 입력 핀 레벨을 바꾼다. 엣지가 맞으면 add_event_detect 콜백을 부른다."""
        level = int(bool(level))
        with self._lock:
            if self._dir.get(pin) != self.IN:
                raise RuntimeError(f"BCM{pin} 는 입력으로 setup 되지 않았다")
            old = self._level[pin]
            self._level[pin] = level
            ev = self._events.get(pin)
        if old == level:
            return
        log.info("[MOCK] set_input(BCM%s, %s)", pin, level)
        if ev is None:
            return
        edge, cb, bounce = ev
        wanted = edge == self.BOTH or (edge == self.RISING and level == 1) or (edge == self.FALLING and level == 0)
        if not wanted or cb is None:
            return
        now = time.monotonic()
        if bounce and now - self._last_cb.get(pin, -1e9) < bounce:
            return
        self._last_cb[pin] = now
        cb(pin)   # 락 밖에서 호출 (콜백이 다시 GPIO 를 건드려도 안전)


GPIO = _MockGPIO()
