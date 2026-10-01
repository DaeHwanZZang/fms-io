"""Fleet 레지스트리 단위 테스트."""

from __future__ import annotations

import time

from common.schemas import ConnectionState, RobotState
from fms_server.fleet import Fleet, RobotLiveness
from fms_server.tests.factories import make_state


def test_state_then_connection_makes_online():
    fleet = Fleet(stale_after=3.0)
    fleet.apply_state(make_state("AMR-001", RobotState.IDLE))
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)

    rec = fleet.get("AMR-001")
    assert rec is not None
    assert rec.liveness(3.0) == RobotLiveness.ONLINE
    assert rec.available_for_job(3.0) is True


def test_connection_only_is_stale_not_online():
    fleet = Fleet()
    fleet.apply_connection("AMR-002", ConnectionState.ONLINE)
    rec = fleet.get("AMR-002")
    assert rec.liveness(3.0) == RobotLiveness.STALE
    assert rec.available_for_job(3.0) is False


def test_stale_state_drops_availability():
    fleet = Fleet(stale_after=0.5)
    fleet.apply_state(make_state("AMR-003", RobotState.IDLE))
    fleet.apply_connection("AMR-003", ConnectionState.ONLINE)
    rec = fleet.get("AMR-003")

    future = time.time() + 5.0
    assert rec.liveness(0.5, now=future) == RobotLiveness.STALE
    assert rec.available_for_job(0.5, now=future) is False


def test_lwt_marks_lost():
    fleet = Fleet()
    fleet.apply_state(make_state("AMR-004", RobotState.IDLE))
    fleet.apply_connection("AMR-004", ConnectionState.ONLINE)
    fleet.apply_connection("AMR-004", ConnectionState.CONNECTION_BROKEN)

    rec = fleet.get("AMR-004")
    assert rec.liveness(3.0) == RobotLiveness.LOST
    assert rec.available_for_job(3.0) is False


def test_busy_robot_not_available():
    fleet = Fleet()
    fleet.apply_state(make_state("AMR-005", RobotState.BUSY, order_id="ORD-1"))
    fleet.apply_connection("AMR-005", ConnectionState.ONLINE)
    rec = fleet.get("AMR-005")
    assert rec.liveness(3.0) == RobotLiveness.ONLINE
    assert rec.available_for_job(3.0) is False


def test_available_robots_filter():
    fleet = Fleet()
    for i, st in enumerate([RobotState.IDLE, RobotState.BUSY, RobotState.IDLE], start=1):
        rid = f"AMR-10{i}"
        fleet.apply_state(make_state(rid, st, order_id="X" if st == RobotState.BUSY else None))
        fleet.apply_connection(rid, ConnectionState.ONLINE)
    available = {r.robot_id for r in fleet.available_robots()}
    assert available == {"AMR-101", "AMR-103"}


def test_listener_fires_and_unsubscribes():
    fleet = Fleet()
    events = []
    unsub = fleet.add_listener(events.append)

    fleet.apply_state(make_state("AMR-201"))
    fleet.apply_connection("AMR-201", ConnectionState.ONLINE)
    assert [e.kind for e in events] == ["state", "connection"]

    unsub()
    fleet.apply_state(make_state("AMR-201", header_id=2))
    assert len(events) == 2  # 해제 후엔 안 온다


def test_forget_removes_and_emits():
    fleet = Fleet()
    fleet.apply_state(make_state("AMR-301"))
    events = []
    fleet.add_listener(events.append)

    assert fleet.forget("AMR-301") is True
    assert fleet.get("AMR-301") is None
    assert events[-1].kind == "removed"
    assert fleet.forget("AMR-301") is False


def test_snapshot_is_copy():
    fleet = Fleet()
    fleet.apply_state(make_state("AMR-401"))
    a = fleet.get("AMR-401")
    fleet.apply_state(make_state("AMR-401", header_id=99, x=5.0))
    assert a.state.header_id == 1  # 이전 스냅샷은 안 바뀐다
    assert fleet.get("AMR-401").state.header_id == 99
