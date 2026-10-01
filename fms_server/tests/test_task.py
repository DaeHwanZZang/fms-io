"""job (move) — JobStore + JobCoordinator 단위 테스트."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from common.schemas import (
    BatteryInfo,
    BusySubState,
    ConnectionState,
    ErrorInfo,
    ErrorLevel,
    Pose,
    RobotState,
    State,
)
from fms_server.fleet import Fleet
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobError, JobStatus, JobStore, compile_to_order
from fms_server.tests.factories import make_state

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")


def _vendor_files() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in VENDOR.iterdir() if p.is_file()}


class FakeMqtt:
    def __init__(self) -> None:
        self.orders = []
        self.instants = []

    def publish_order(self, order) -> None:
        self.orders.append(order)

    def publish_instant(self, action) -> None:
        self.instants.append(action)


@pytest.fixture
def env(tmp_path):
    """hq / floor-1(active) / AMR-001 등록 / location 'dest' + fleet 에 로봇 online."""
    registry = SiteRegistry(tmp_path)
    registry.create_site("hq", "본사")
    result = registry.map_store("hq").ingest_upload("floor-1", _vendor_files(), activate=True)
    version = result.version
    registry.register_robot("hq", "AMR-001", "floor-1")
    registry.location_store("hq").create("dest", "floor-1", 3.0, 4.0, 0.0)
    registry.location_store("hq").create("dest2", "floor-1", 5.0, 6.0, 1.0)

    fleet = Fleet(stale_after=3.0)
    mqtt = FakeMqtt()
    store = JobStore(tmp_path)
    coord = JobCoordinator(fleet, registry, store, mqtt)
    coord.start()

    def robot_online(robot_id="AMR-001", state=RobotState.IDLE, order_id=None, mv=version):
        fleet.apply_connection(robot_id, ConnectionState.ONLINE)
        fleet.apply_state(
            make_state(robot_id, state, order_id=order_id, map_id="uid", map_version=mv)
        )

    robot_online()
    yield dict(
        registry=registry, fleet=fleet, mqtt=mqtt, store=store, coord=coord,
        version=version, robot_online=robot_online,
    )
    coord.stop()


# =============================================================================
# JobStore
# =============================================================================

def test_create_job_pending(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    assert job.status == JobStatus.PENDING
    assert job.job_id == "hq-J0001"
    assert len(job.commands) == 1


def test_create_rejects_unknown_command(env):
    with pytest.raises(JobError):
        env["store"].create("hq", [{"type": "fly", "target": "x"}], "AMR-001")
    with pytest.raises(JobError):
        env["store"].create("hq", [{"type": "docking", "target": "x"}], "AMR-001")


def test_create_rejects_empty(env):
    with pytest.raises(JobError):
        env["store"].create("hq", [], "AMR-001")


def test_job_ids_increment_per_site(env):
    a = env["store"].create("hq", [{"type": "move", "target": "dest"}])
    b = env["store"].create("hq", [{"type": "move", "target": "dest"}])
    assert (a.job_id, b.job_id) == ("hq-J0001", "hq-J0002")


def test_persistence_reload(env, tmp_path):
    env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    reloaded = JobStore(tmp_path)
    jobs = reloaded.list("hq")
    assert len(jobs) == 1 and jobs[0].commands[0].target == "dest"


# =============================================================================
# compile_to_order
# =============================================================================

def test_compile_builds_nodes_in_order(env):
    job = env["store"].create(
        "hq",
        [{"type": "move", "target": "dest"}, {"type": "move", "target": "dest2"}],
        "AMR-001",
    )
    job.order_id = job.job_id
    order = compile_to_order(job, env["registry"])
    assert order.robot_id == "AMR-001"
    assert [n.sequence_id for n in order.nodes] == [0, 1]
    assert all(n.released for n in order.nodes)
    assert (order.nodes[0].position.x, order.nodes[0].position.y) == (3.0, 4.0)
    assert order.nodes[1].position.theta == 1.0


def test_compile_keep_theta_adds_wait_on_intermediate_only(env):
    """keep_theta 는 중간 노드에만 WAIT 액션(구간 분리)을 붙이고, 마지막 노드/일반 노드엔 붙이지 않는다."""
    store = env["registry"].location_store("hq")
    store.update("dest", keep_theta=True)
    store.update("dest2", keep_theta=True)
    job = env["store"].create(
        "hq",
        [{"type": "move", "target": "dest"}, {"type": "move", "target": "dest2"},
         {"type": "move", "target": "dest"}],
        "AMR-001",
    )
    job.order_id = job.job_id
    nodes = compile_to_order(job, env["registry"]).nodes
    assert [n.action is not None for n in nodes] == [True, True, False]
    assert nodes[0].action.action_type.value == "WAIT"
    assert len({n.action.action_id for n in nodes if n.action}) == 2

    store.update("dest", keep_theta=False)
    nodes = compile_to_order(job, env["registry"]).nodes
    assert [n.action is not None for n in nodes] == [False, True, False]


def test_location_keep_theta_persists_and_defaults_false(env):
    store = env["registry"].location_store("hq")
    assert store.get("dest").keep_theta is False
    store.update("dest", keep_theta=True)
    assert store.get("dest").keep_theta is True
    store.update("dest", x=9.0)            # 다른 필드 수정이 플래그를 지우지 않는다
    assert store.get("dest").keep_theta is True


def test_compile_rejects_unknown_location(env):
    job = env["store"].create("hq", [{"type": "move", "target": "ghost"}], "AMR-001")
    with pytest.raises(JobError):
        compile_to_order(job, env["registry"])


def test_compile_rejects_wrong_map(env):
    env["registry"].map_store("hq").ingest_upload("floor-2", _vendor_files(), activate=True)
    env["registry"].location_store("hq").create("f2", "floor-2", 1.0, 2.0)
    job = env["store"].create("hq", [{"type": "move", "target": "f2"}], "AMR-001")
    with pytest.raises(JobError):  # 로봇 배정 맵은 floor-1
        compile_to_order(job, env["registry"])


# =============================================================================
# dispatch
# =============================================================================

def test_dispatch_publishes_order_and_goes_working(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    out = env["coord"].dispatch(job.job_id)
    assert out.status == JobStatus.WORKING
    assert len(env["mqtt"].orders) == 1
    assert env["mqtt"].orders[0].order_id == job.job_id
    assert env["mqtt"].orders[0].task_id == job.job_id


def test_dispatch_rejects_busy_robot(env):
    env["robot_online"](state=RobotState.BUSY)
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)
    assert env["store"].get(job.job_id).status == JobStatus.PENDING


def test_dispatch_rejects_stale_map_version(env):
    env["robot_online"](mv="old000000000")
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)


def test_dispatch_rejects_unknown_robot_state(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-002")
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)


def test_double_dispatch_rejected(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)


# =============================================================================
# 로봇 보고 -> job 상태
# =============================================================================

def _report(fleet, robot_id, *, state, order_id, version, errors=None, last_seq=0):
    fleet.apply_state(
        State(
            header_id=9,
            timestamp=time.time(),
            robot_id=robot_id,
            state=state,
            sub_state=BusySubState.MOVING if state == RobotState.BUSY else None,
            pose=Pose(x=0.0, y=0.0, theta=0.0),
            last_node_id="n0",
            last_node_sequence_id=last_seq,
            order_id=order_id,
            battery=BatteryInfo(charge=80.0),
            map_id="uid",
            map_version=version,
            errors=errors or [],
        )
    )


def test_job_succeeds_when_robot_clears_order(env):
    v = env["version"]
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)

    _report(env["fleet"], "AMR-001", state=RobotState.BUSY, order_id=job.job_id, version=v)
    assert env["store"].get(job.job_id).status == JobStatus.WORKING
    assert env["store"].get(job.job_id).acknowledged is True

    _report(env["fleet"], "AMR-001", state=RobotState.IDLE, order_id=None, version=v)
    done = env["store"].get(job.job_id)
    assert done.status == JobStatus.SUCCESS
    assert done.done_commands == 1


def test_job_fails_on_fatal_error(env):
    v = env["version"]
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    _report(env["fleet"], "AMR-001", state=RobotState.BUSY, order_id=job.job_id, version=v)
    _report(
        env["fleet"], "AMR-001", state=RobotState.ERROR, order_id=None, version=v,
        errors=[ErrorInfo(error_type="NO_PATH", level=ErrorLevel.FATAL)],
    )
    done = env["store"].get(job.job_id)
    assert done.status == JobStatus.FAILED
    assert "NO_PATH" in done.error


def test_order_clear_before_ack_does_not_succeed(env):
    """디스패치 직후 로봇이 아직 오더를 못 물었을 때 order_id=None 을 완료로 보면 안 된다."""
    v = env["version"]
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    _report(env["fleet"], "AMR-001", state=RobotState.IDLE, order_id=None, version=v)
    assert env["store"].get(job.job_id).status == JobStatus.WORKING


# =============================================================================
# cancel
# =============================================================================

def test_cancel_working_publishes_instant(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    out = env["coord"].cancel(job.job_id)
    assert out.status == JobStatus.CANCELED
    assert len(env["mqtt"].instants) == 1
    assert env["mqtt"].instants[0].action_type.value == "CANCEL_ORDER"


def test_cancel_pending_no_instant(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].cancel(job.job_id)
    assert env["mqtt"].instants == []


def test_cancel_terminal_rejected(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].cancel(job.job_id)
    with pytest.raises(JobError):
        env["coord"].cancel(job.job_id)


# =============================================================================
# [homework_iot 추가] 큐 자동 배정 — working 중에도 pending 생성, 먼저 만든 순서, 로봇 지정/미지정
# =============================================================================

def test_pending_job_auto_dispatched_when_robot_idle(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}])   # 로봇 미지정
    env["robot_online"]()                                                    # IDLE state 도착
    out = env["store"].get(job.job_id)
    assert out.status == JobStatus.WORKING and out.robot_id == "AMR-001"


def test_queue_is_fifo_and_waits_while_working(env):
    v = env["version"]
    a = env["store"].create("hq", [{"type": "move", "target": "dest"}])
    b = env["store"].create("hq", [{"type": "move", "target": "dest2"}])
    env["robot_online"]()
    assert env["store"].get(a.job_id).status == JobStatus.WORKING
    assert env["store"].get(b.job_id).status == JobStatus.PENDING
    _report(env["fleet"], "AMR-001", state=RobotState.BUSY, order_id=a.job_id, version=v)
    assert env["store"].get(b.job_id).status == JobStatus.PENDING
    _report(env["fleet"], "AMR-001", state=RobotState.IDLE, order_id=None, version=v)
    assert env["store"].get(a.job_id).status == JobStatus.SUCCESS
    assert env["store"].get(b.job_id).status == JobStatus.WORKING   # 끝나자마자 다음 job
    assert [o.order_id for o in env["mqtt"].orders] == [a.job_id, b.job_id]


def test_job_pinned_to_other_robot_is_skipped(env):
    pinned = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-002")
    anyone = env["store"].create("hq", [{"type": "move", "target": "dest2"}])
    env["robot_online"]()
    assert env["store"].get(pinned.job_id).status == JobStatus.PENDING
    assert env["store"].get(anyone.job_id).status == JobStatus.WORKING


# =============================================================================
# [homework_iot 추가] wait / wait_flag — job 이 wait 에서 쪼개져 구간별 Order 로 나간다
# =============================================================================

def _make_coord(env, flags):
    env["coord"].stop()
    coord = JobCoordinator(env["fleet"], env["registry"], env["store"], env["mqtt"], flags)
    coord.start()
    env["coord"] = coord
    return coord


def _finish_order(env, order_id):
    v = env["version"]
    _report(env["fleet"], "AMR-001", state=RobotState.BUSY, order_id=order_id, version=v)
    _report(env["fleet"], "AMR-001", state=RobotState.IDLE, order_id=None, version=v)


def test_wait_command_validation(env):
    for bad in ({"type": "wait"}, {"type": "wait", "seconds": 0}, {"type": "wait", "seconds": 99999},
                {"type": "wait_flag"}, {"type": "wait_flag", "flag": "a/b"}):
        with pytest.raises(JobError):
            env["store"].create("hq", [bad])


def test_wait_seconds_splits_orders(env, monkeypatch):
    import fms_server.task as task
    now = [1000.0]
    monkeypatch.setattr(task.time, "time", lambda: now[0])
    job = env["store"].create(
        "hq",
        [{"type": "move", "target": "dest"}, {"type": "wait", "seconds": 5},
         {"type": "move", "target": "dest2"}],
        "AMR-001",
    )
    env["coord"].dispatch(job.job_id)
    assert [len(o.nodes) for o in env["mqtt"].orders] == [1]
    _finish_order(env, job.job_id)
    j = env["store"].get(job.job_id)
    assert j.status == JobStatus.WORKING and j.phase == "wait" and j.done_commands == 1
    assert len(env["mqtt"].orders) == 1                      # 대기 중엔 새 Order 없음
    now[0] += 4.9
    env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"
    now[0] += 0.2
    env["robot_online"]()
    assert [o.order_id for o in env["mqtt"].orders] == [job.job_id, f"{job.job_id}~c2"]
    _finish_order(env, f"{job.job_id}~c2")
    assert env["store"].get(job.job_id).status == JobStatus.SUCCESS


def test_wait_flag_blocks_until_lowered(env):
    from fms_server.flags import FlagStore
    flags = FlagStore()
    _make_coord(env, flags)
    job = env["store"].create(
        "hq", [{"type": "move", "target": "dest"}, {"type": "wait_flag", "flag": "door"},
               {"type": "move", "target": "dest2"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    _finish_order(env, job.job_id)
    env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"       # 신호 없음(unknown) -> 대기
    flags.set("door", True)
    env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"       # 올라가 있음 -> 대기
    flags.set("door", False)
    env["robot_online"]()
    assert len(env["mqtt"].orders) == 2
    assert env["store"].get(job.job_id).phase == "order"


def test_trailing_wait_ends_job_and_holds_robot(env, monkeypatch):
    import fms_server.task as task
    now = [0.0]
    monkeypatch.setattr(task.time, "time", lambda: now[0])
    a = env["store"].create("hq", [{"type": "wait", "seconds": 2}], "AMR-001")
    b = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(a.job_id)
    env["robot_online"]()
    assert env["store"].get(b.job_id).status == JobStatus.PENDING   # wait 중엔 로봇 점유
    now[0] += 3
    env["robot_online"]()
    assert env["store"].get(a.job_id).status == JobStatus.SUCCESS
    assert env["store"].get(b.job_id).status == JobStatus.WORKING


def test_wait_flag_until_raised(env):
    from fms_server.flags import FlagStore
    flags = FlagStore()
    _make_coord(env, flags)
    job = env["store"].create(
        "hq", [{"type": "wait_flag", "flag": "button", "until": "raised"},
               {"type": "move", "target": "dest"}], "AMR-001")
    assert job.commands[0].until == "raised"
    env["coord"].dispatch(job.job_id)
    env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"       # unknown -> 대기
    flags.set("button", False)
    env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"       # 내려가 있음 -> 계속 대기
    flags.set("button", True)
    env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "order"
    with pytest.raises(JobError):
        env["store"].create("hq", [{"type": "wait_flag", "flag": "x", "until": "never"}])


# =============================================================================
# [homework_iot 추가] set_output — 파이 핀 출력 명령 (도착 -> 신호 -> 다음 이동)
# =============================================================================

class FakeIo:
    def __init__(self, fail: str | None = None) -> None:
        self.calls = []
        self.fail = fail

    def set_output(self, device, value):
        from fms_server.io_client import IoError
        if self.fail:
            raise IoError(self.fail)
        self.calls.append((device, value))


def _wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _make_coord_io(env, io):
    env["coord"].stop()
    coord = JobCoordinator(env["fleet"], env["registry"], env["store"], env["mqtt"], None, io)
    coord.start()
    env["coord"] = coord


def test_set_output_validation(env):
    for bad in ({"type": "set_output"}, {"type": "set_output", "device": "lamp_red"},
                {"type": "set_output", "device": "lamp_red", "value": 2},
                {"type": "set_output", "device": "a/b", "value": 1}):
        with pytest.raises(JobError):
            env["store"].create("hq", [bad])
    job = env["store"].create("hq", [{"type": "set_output", "device": "lamp_red", "value": True}])
    assert job.commands[0].to_dict() == {"type": "set_output", "device": "lamp_red", "value": 1}


def test_arrive_signal_then_move(env):
    io = FakeIo()
    _make_coord_io(env, io)
    job = env["store"].create(
        "hq", [{"type": "move", "target": "dest"},
               {"type": "set_output", "device": "lamp_red", "value": 1},
               {"type": "move", "target": "dest2"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    assert io.calls == []                                   # 도착 전엔 신호 안 보냄
    _finish_order(env, job.job_id)
    assert _wait_for(lambda: env["store"].get(job.job_id).phase == "order" and len(env["mqtt"].orders) == 2)
    assert io.calls == [("lamp_red", 1)]                    # 도착 후 신호 -> 다음 구간 Order
    assert env["mqtt"].orders[1].order_id == f"{job.job_id}~c2"
    _finish_order(env, f"{job.job_id}~c2")
    assert env["store"].get(job.job_id).status == JobStatus.SUCCESS


def test_set_output_failure_fails_job(env):
    _make_coord_io(env, FakeIo(fail="파이 꺼짐"))
    job = env["store"].create("hq", [{"type": "set_output", "device": "lamp_red", "value": 1}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    assert _wait_for(lambda: env["store"].get(job.job_id).status == JobStatus.FAILED)
    assert "파이 꺼짐" in env["store"].get(job.job_id).error


def test_set_output_only_job_succeeds(env):
    io = FakeIo()
    _make_coord_io(env, io)
    job = env["store"].create("hq", [{"type": "set_output", "device": "buzzer", "value": 0}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    assert _wait_for(lambda: env["store"].get(job.job_id).status == JobStatus.SUCCESS)
    assert io.calls == [("buzzer", 0)]


# =============================================================================
# [homework_iot 추가] wait_flag 조건: hold / timeout / 신호 만료
# =============================================================================

def _flag_job(env, flags, **cond):
    _make_coord(env, flags)
    job = env["store"].create(
        "hq", [{"type": "wait_flag", "flag": "button", "until": "lowered", **cond},
               {"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    return job


def test_wait_flag_hold_requires_continuous_condition(env, monkeypatch):
    import fms_server.task as task
    from fms_server.flags import FlagStore
    now = [1000.0]
    monkeypatch.setattr(task.time, "time", lambda: now[0])
    flags = FlagStore()
    job = _flag_job(env, flags, hold=2)
    flags.set("button", False); env["robot_online"]()          # 조건 참 시작
    now[0] += 1.5; flags.set("button", False); env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"          # 1.5s < hold
    now[0] += 0.1; flags.set("button", True); env["robot_online"]()   # 튀어서 끊김 -> 타이머 리셋
    now[0] += 0.1; flags.set("button", False); env["robot_online"]()
    now[0] += 1.9; flags.set("button", False); env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "wait"          # 리셋 뒤 1.9s
    now[0] += 0.2; flags.set("button", False); env["robot_online"]()
    assert env["store"].get(job.job_id).phase == "order"         # 2.1s 유지 -> 통과


def test_wait_flag_timeout_fail_and_continue(env, monkeypatch):
    import fms_server.task as task
    from fms_server.flags import FlagStore
    now = [1000.0]
    monkeypatch.setattr(task.time, "time", lambda: now[0])
    flags = FlagStore()
    job = _flag_job(env, flags, timeout=10)                       # 기본 on_timeout=fail
    flags.set("button", True)
    now[0] += 9; flags.set("button", True); env["robot_online"]()
    assert env["store"].get(job.job_id).status == JobStatus.WORKING
    now[0] += 2; flags.set("button", True); env["robot_online"]()
    j = env["store"].get(job.job_id)
    assert j.status == JobStatus.FAILED and "시간 초과" in j.error

    job2 = _flag_job(env, flags, timeout=5, on_timeout="continue")
    now[0] += 6; flags.set("button", True); env["robot_online"]()
    assert env["store"].get(job2.job_id).phase == "order"         # 시간 초과해도 계속 진행


def test_wait_flag_stale_signal_does_not_satisfy(env, monkeypatch):
    import fms_server.task as task
    from fms_server.flags import FlagStore
    now = [1000.0]
    monkeypatch.setattr(task.time, "time", lambda: now[0])
    flags = FlagStore()
    job = _flag_job(env, flags)
    flags.set("button", False)                                  # 파이가 마지막으로 보낸 값
    now[0] += task.FLAG_STALE_SEC + 1; env["robot_online"]()      # 그 뒤 소식 없음 = 파이 끊김
    assert env["store"].get(job.job_id).phase == "wait"
    flags.set("button", False); env["robot_online"]()           # 다시 연결
    assert env["store"].get(job.job_id).phase == "order"


def test_wait_flag_option_validation(env):
    for bad in ({"hold": -1}, {"hold": "x"}, {"timeout": 0}, {"on_timeout": "ignore"}):
        with pytest.raises(JobError):
            env["store"].create("hq", [{"type": "wait_flag", "flag": "x", **bad}])
    c = env["store"].create("hq", [{"type": "wait_flag", "flag": "x"}]).commands[0].to_dict()
    assert (c["hold"], c["timeout"], c["on_timeout"]) == (0.0, None, "fail")
