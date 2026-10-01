"""테스트용 메시지 팩토리. 로봇이 보냈을 법한 최소 유효 페이로드를 만든다."""

from __future__ import annotations

import time

from common.schemas import (
    BatteryInfo,
    BusySubState,
    Connection,
    ConnectionState,
    Pose,
    RobotState,
    State,
)


def make_state(
    robot_id: str = "AMR-001",
    state: RobotState = RobotState.IDLE,
    *,
    header_id: int = 1,
    x: float = 1.0,
    y: float = 2.0,
    order_id: str | None = None,
    battery: float = 90.0,
    map_id: str | None = None,
    map_version: str | None = None,
) -> State:
    sub_state = BusySubState.MOVING if state == RobotState.BUSY else None
    return State(
        header_id=header_id,
        timestamp=time.time(),
        robot_id=robot_id,
        state=state,
        sub_state=sub_state,
        pose=Pose(x=x, y=y, theta=0.0),
        last_node_id="n0",
        order_id=order_id,
        battery=BatteryInfo(charge=battery),
        map_id=map_id,
        map_version=map_version,
    )


def make_connection(
    robot_id: str = "AMR-001",
    connection_state: ConnectionState = ConnectionState.ONLINE,
    *,
    header_id: int = 1,
) -> Connection:
    return Connection(
        header_id=header_id,
        timestamp=time.time(),
        robot_id=robot_id,
        connection_state=connection_state,
    )
