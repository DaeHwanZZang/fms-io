"""
로봇 상태 레지스트리
===================

`mqtt_client` 가 수신한 최신 `State` / `Connection` 을 robot_id 별로 보관한다.
**FMS 가 아는 로봇 상태는 이게 전부** — 로봇을 직접 시뮬레이션하지 않는다
(CLAUDE.md 3번 원칙).

스레드 안전성
-------------
paho-mqtt 콜백은 자체 네트워크 스레드에서 돈다. FastAPI 핸들러는 asyncio
이벤트 루프에서 돈다. 둘이 같은 `Fleet` 을 만지므로 모든 접근을 `RLock` 으로
감싼다. 스냅샷은 항상 복사본을 돌려준다 (호출자가 lock 밖에서 자유롭게 읽도록).

변경 알림
---------
`add_listener(cb)` 로 등록하면 state/connection 이 바뀔 때마다 `FleetEvent` 로
콜백된다. 콜백은 **paho 스레드에서 호출될 수 있으므로** 그 안에서 블로킹하거나
asyncio 객체를 직접 만지면 안 된다 — 이벤트를 큐에 넣고 빠져나와야 한다
(`api/robots.py` 의 WebSocket 브로드캐스터가 그 방식).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from common.schemas import ConnectionState, State


class RobotLiveness(str, Enum):
    """레지스트리가 보는 로봇의 생존 상태. `RobotState` 와는 다른 축이다."""

    ONLINE = "ONLINE"          # connection ONLINE + 최근에 state 를 받음
    STALE = "STALE"            # connection 은 ONLINE 인데 state 가 끊긴 지 오래
    OFFLINE = "OFFLINE"        # 정상 종료 (Connection.OFFLINE)
    LOST = "LOST"              # LWT 로 브로커가 CONNECTION_BROKEN 발행
    UNKNOWN = "UNKNOWN"        # state 만 받았고 connection 소식이 없음
    UNREGISTERED = "UNREGISTERED"  # 접속은 했지만 어느 site 에도 등록 안 됨.
                                   # Fleet 자체는 이 값을 만들지 않는다 — site 정보를
                                   # 아는 뷰/코디네이터 계층이 덧씌운다 (resolve_liveness)


@dataclass
class RobotRecord:
    """로봇 하나에 대해 FMS 가 아는 전부."""

    robot_id: str
    state: Optional[State] = None
    connection: Optional[ConnectionState] = None

    first_seen: float = field(default_factory=time.time)
    last_state_at: Optional[float] = None
    last_connection_at: Optional[float] = None

    def liveness(self, stale_after: float, now: Optional[float] = None) -> RobotLiveness:
        now = time.time() if now is None else now

        if self.connection == ConnectionState.CONNECTION_BROKEN:
            return RobotLiveness.LOST
        if self.connection == ConnectionState.OFFLINE:
            return RobotLiveness.OFFLINE

        if self.last_state_at is None:
            # connection ONLINE 은 봤지만 아직 state 가 없다.
            return RobotLiveness.UNKNOWN if self.connection is None else RobotLiveness.STALE

        fresh = (now - self.last_state_at) <= stale_after
        if self.connection == ConnectionState.ONLINE:
            return RobotLiveness.ONLINE if fresh else RobotLiveness.STALE
        # connection 소식이 아예 없는데 state 는 들어오는 경우 (retain 유실 등).
        return RobotLiveness.ONLINE if fresh else RobotLiveness.UNKNOWN

    def available_for_job(self, stale_after: float, now: Optional[float] = None) -> bool:
        """새 작업을 받을 수 있나. 할당기(allocator)가 보는 술어.

        맵 버전 게이팅은 여기서 하지 않는다 — 활성 맵을 아는 건 allocator 다.
        """
        if self.state is None:
            return False
        if self.liveness(stale_after, now) != RobotLiveness.ONLINE:
            return False
        return self.state.is_available


@dataclass
class FleetEvent:
    kind: str            # "state" | "connection" | "removed"
    robot_id: str
    record: RobotRecord  # 이벤트 시점의 스냅샷 (복사본)


ListenerCallback = Callable[[FleetEvent], None]


class Fleet:
    """robot_id -> RobotRecord 레지스트리. 스레드 안전."""

    def __init__(self, stale_after: float = 3.0) -> None:
        self._lock = threading.RLock()
        self._robots: dict[str, RobotRecord] = {}
        self._listeners: list[ListenerCallback] = []
        self.stale_after = stale_after

    # -- 갱신 (mqtt_client 가 호출) ---------------------------------------

    def apply_state(self, state: State) -> None:
        now = time.time()
        with self._lock:
            record = self._robots.setdefault(state.robot_id, RobotRecord(robot_id=state.robot_id))
            record.state = state
            record.last_state_at = now
            snapshot = _copy_record(record)
        self._emit(FleetEvent(kind="state", robot_id=state.robot_id, record=snapshot))

    def apply_connection(self, robot_id: str, connection: ConnectionState) -> None:
        now = time.time()
        with self._lock:
            record = self._robots.setdefault(robot_id, RobotRecord(robot_id=robot_id))
            record.connection = connection
            record.last_connection_at = now
            snapshot = _copy_record(record)
        self._emit(FleetEvent(kind="connection", robot_id=robot_id, record=snapshot))

    def forget(self, robot_id: str) -> bool:
        """레지스트리에서 로봇을 지운다 (테스트/운영 정리용)."""
        with self._lock:
            record = self._robots.pop(robot_id, None)
            if record is None:
                return False
            snapshot = _copy_record(record)
        self._emit(FleetEvent(kind="removed", robot_id=robot_id, record=snapshot))
        return True

    # -- 조회 (API 가 호출) ---------------------------------------------

    def get(self, robot_id: str) -> Optional[RobotRecord]:
        with self._lock:
            record = self._robots.get(robot_id)
            return _copy_record(record) if record is not None else None

    def all(self) -> list[RobotRecord]:
        with self._lock:
            return [_copy_record(r) for r in self._robots.values()]

    def available_robots(self, now: Optional[float] = None) -> list[RobotRecord]:
        with self._lock:
            return [
                _copy_record(r)
                for r in self._robots.values()
                if r.available_for_job(self.stale_after, now)
            ]

    # -- 리스너 -------------------------------------------------------------

    def add_listener(self, callback: ListenerCallback) -> Callable[[], None]:
        """변경 리스너 등록. 해제 함수를 돌려준다."""
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, event: FleetEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(event)
            except Exception:  # 리스너 하나가 죽어도 레지스트리는 계속 돈다
                import logging

                logging.getLogger("fms.fleet").exception("fleet listener 콜백 실패")


def _copy_record(record: RobotRecord) -> RobotRecord:
    """RobotRecord 얕은 복사. State/ConnectionState 는 불변이라 공유해도 안전."""
    return RobotRecord(
        robot_id=record.robot_id,
        state=record.state,
        connection=record.connection,
        first_seen=record.first_seen,
        last_state_at=record.last_state_at,
        last_connection_at=record.last_connection_at,
    )
