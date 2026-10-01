"""
job (작업) — 로봇이 수행할 명령의 순서 묶음
==========================================

**job 은 FMS 안에만 있다.** 로봇은 job 을 모르고 `Order`(노드 목록)만 받는다.
allocator/coordinator 가 job 의 명령을 `Order` 로 컴파일해서 발행하고, 로봇이
보고한 `State` 로 job 상태를 되짚는다.

    job.commands = [move(loc_A), move(loc_B), ...]   (지금은 move 만)
        -> Order(nodes=[node@pose_A, node@pose_B, ...], 전부 released=true)

명령 종류
    move      : named location 하나로 이동 (target = location 이름)
    wait      : [homework_iot 추가] seconds 초 동안 아무것도 안 하고 대기
    set_output: [homework_iot 추가] 라즈베리파이 출력 핀 제어 (device = io_config 의 이름, value = 0/1).
                예) loc1 도착 -> set_output lamp_red 1 -> loc2 이동. 파이가 응답하지 않으면 job 은 failed.
    wait_flag : [homework_iot 추가] 외부 플래그(flags.py, 라즈베리파이 신호)가 내려갈 때까지(until=lowered,
                기본) 또는 올라갈 때까지(until=raised) 대기. 선택 옵션: hold(초, 그 시간 끊김 없이 유지돼야 통과),
                timeout(초) + on_timeout(fail|continue). 한 번도 못 받았거나 FLAG_STALE_SEC 넘게 갱신이 없는
                플래그는 조건 불충족(계속 대기)이다.
    docking   : (미구현)
    charging  : (미구현)

wait/wait_flag 는 로봇이 모르는 FMS 쪽 명령이다. job 은 wait 명령을 경계로 쪼개져 연속 move
구간마다 Order 하나(order_id = job_id, 이후 구간은 `{job_id}~c{시작 명령 번호}`)를 발행하고,
wait 동안 로봇은 IDLE 이지만 job 이 working 이라 다른 job 에 배정되지 않는다 (로봇 점유).

job 상태
    pending   생성됨, 아직 로봇에 안 내려감
    working   Order 발행됨, 로봇이 수행 중
    success   로봇이 Order 를 끝내고 order_id 를 비웠다 (FATAL 없이)
    failed    로봇이 FATAL 오류 / ERROR 상태 보고
    canceled  관리자가 취소 (working 이면 CANCEL_ORDER instant 발행)

영속화: `{data_root}/sites/{site_id}/jobs.json` — `{seq, jobs:{job_id:{...}}}`.
`JobStore` 가 모든 site 의 job 을 메모리에 들고 있는다 (SiteRegistry 와 같은 방식).
로봇 state 마다 디스크를 읽지 않으려는 것 — `JobCoordinator` 는 메모리 인덱스로
동작하고 변경 시에만 파일에 쓴다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from common.schemas import (
    Action,
    ActionType,
    ErrorLevel,
    InstantAction,
    InstantActionType,
    Order,
    OrderNode,
    RobotState,
    State,
)
from fms_server.fleet import Fleet, FleetEvent
from fms_server.io_client import IoError, PiIoClient
from fms_server.flags import FlagError, FlagStore, check_flag_name
from fms_server.locations import LocationError, LocationNotFound
from fms_server.mqtt_client import FmsMqttClient
from fms_server.sites import SiteRegistry

log = logging.getLogger("fms.task")

JOBS_FILE = "jobs.json"


class JobError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class JobNotFound(JobError):
    """job 을 못 찾음 (HTTP 404 급)."""


class JobStatus(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_seg(value: str, kind: str) -> None:
    if not value or "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise JobError(f"허용되지 않는 {kind}: {value!r}")


# =============================================================================
# 명령
# =============================================================================

@dataclass
class MoveCommand:
    """named location 하나로 이동."""

    target: str  # location 이름 (site 스코프)

    kind: str = "move"

    def to_dict(self) -> dict:
        return {"type": "move", "target": self.target}


@dataclass
class WaitCommand:
    """[homework_iot 추가] 지정 시간 대기."""

    seconds: float

    kind: str = "wait"

    def to_dict(self) -> dict:
        return {"type": "wait", "seconds": self.seconds}


@dataclass
class WaitFlagCommand:
    """[homework_iot 추가] 외부 플래그가 내려갈 때까지 대기."""

    flag: str
    until: str = "lowered"        # "lowered"(내려갈 때까지, 기본) | "raised"(올라갈 때까지)
    hold: float = 0.0             # 조건이 이 시간(초) 동안 끊김 없이 유지돼야 통과 (센서가 잠깐 튀는 것 무시)
    timeout: Optional[float] = None   # 이 시간(초) 안에 통과 못 하면 on_timeout 대로 (None = 무한 대기)
    on_timeout: str = "fail"      # "fail"(job 실패) | "continue"(그냥 다음 단계로)

    kind: str = "wait_flag"

    def to_dict(self) -> dict:
        return {"type": "wait_flag", "flag": self.flag, "until": self.until,
                "hold": self.hold, "timeout": self.timeout, "on_timeout": self.on_timeout}


@dataclass
class SetOutputCommand:
    """[homework_iot 추가] 라즈베리파이 출력 핀 제어 (FMS 가 파이에 HTTP 로 요청)."""

    device: str
    value: int   # 0 | 1

    kind: str = "set_output"

    def to_dict(self) -> dict:
        return {"type": "set_output", "device": self.device, "value": self.value}


MAX_WAIT_SEC = 3600
MAX_TIMEOUT_SEC = 86400
FLAG_STALE_SEC = 6.0   # 파이는 2초마다 재전송한다. 이보다 오래 소식이 없으면 신호원이 끊긴 것으로 본다


def _parse_command(entry: dict):
    ctype = entry.get("type")
    if ctype == "wait":
        try:
            seconds = float(entry.get("seconds"))
        except (TypeError, ValueError):
            raise JobError("wait 명령에 seconds(숫자)가 없다") from None
        if not (0 < seconds <= MAX_WAIT_SEC):
            raise JobError(f"wait seconds 는 0 초과 {MAX_WAIT_SEC} 이하여야 한다")
        return WaitCommand(seconds=seconds)
    if ctype == "set_output":
        device, value = entry.get("device"), entry.get("value")
        if not device or not isinstance(device, str):
            raise JobError("set_output 명령에 device(핀 장치 이름)가 없다")
        try:
            check_flag_name(device)
        except FlagError as exc:
            raise JobError(str(exc)) from None
        if value not in (0, 1, True, False):
            raise JobError("set_output value 는 0 또는 1")
        return SetOutputCommand(device=device, value=int(value))
    if ctype == "wait_flag":
        flag = entry.get("flag")
        if not flag or not isinstance(flag, str):
            raise JobError("wait_flag 명령에 flag(이름)가 없다")
        try:
            check_flag_name(flag)
        except FlagError as exc:
            raise JobError(str(exc)) from None
        until = entry.get("until") or "lowered"
        if until not in ("lowered", "raised"):
            raise JobError("wait_flag until 은 'lowered' 또는 'raised'")
        try:
            hold = float(entry.get("hold") or 0.0)
            timeout_raw = entry.get("timeout")
            timeout = float(timeout_raw) if timeout_raw not in (None, "") else None
        except (TypeError, ValueError):
            raise JobError("wait_flag hold/timeout 은 숫자(초)") from None
        if not (0 <= hold <= MAX_WAIT_SEC):
            raise JobError(f"wait_flag hold 는 0 이상 {MAX_WAIT_SEC} 이하")
        if timeout is not None and not (0 < timeout <= MAX_TIMEOUT_SEC):
            raise JobError(f"wait_flag timeout 은 0 초과 {MAX_TIMEOUT_SEC} 이하")
        on_timeout = entry.get("on_timeout") or "fail"
        if on_timeout not in ("fail", "continue"):
            raise JobError("wait_flag on_timeout 은 'fail' 또는 'continue'")
        return WaitFlagCommand(flag=flag, until=until, hold=hold, timeout=timeout, on_timeout=on_timeout)
    if ctype == "move":
        target = entry.get("target")
        if not target or not isinstance(target, str):
            raise JobError("move 명령에 target(location 이름)이 없다")
        return MoveCommand(target=target)
    if ctype in ("docking", "charging"):
        raise JobError(f"{ctype} 명령은 아직 미구현")
    raise JobError(f"알 수 없는 명령 type: {ctype!r}")


# =============================================================================
# Job
# =============================================================================

@dataclass
class Job:
    job_id: str
    site_id: str
    commands: list
    status: JobStatus = JobStatus.PENDING
    robot_id: Optional[str] = None

    order_id: Optional[str] = None
    order_update_id: int = 0
    done_commands: int = 0        # 완료로 간주한 명령 수 (진행률 표시용)
    acknowledged: bool = False    # 로봇이 우리 order_id 를 한 번이라도 보고했나
    # [homework_iot 추가] wait 로 쪼개진 실행 위치
    cursor: int = 0               # 지금 수행 중인 구간/명령의 시작 번호 (이 앞은 끝남)
    order_end: int = 0            # phase=="order" 일 때 발행한 Order 가 덮는 명령 범위의 끝(미포함)
    phase: Optional[str] = None   # "order"(로봇 수행 중) | "wait"(cursor 의 wait 명령 대기 중) | None
    wait_started: Optional[float] = None
    error: Optional[str] = None

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    dispatched_at: Optional[str] = None

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "commands": [c.to_dict() for c in self.commands],
            "status": self.status.value,
            "robot_id": self.robot_id,
            "order_id": self.order_id,
            "order_update_id": self.order_update_id,
            "done_commands": self.done_commands,
            "acknowledged": self.acknowledged,
            "cursor": self.cursor,
            "order_end": self.order_end,
            "phase": self.phase,
            "wait_started": self.wait_started,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dispatched_at": self.dispatched_at,
        }

    @classmethod
    def from_dict(cls, job_id: str, data: dict) -> "Job":
        return cls(
            job_id=job_id,
            site_id=data["site_id"],
            commands=[_parse_command(e) for e in data.get("commands", [])],
            status=JobStatus(data.get("status", "pending")),
            robot_id=data.get("robot_id"),
            order_id=data.get("order_id"),
            order_update_id=data.get("order_update_id", 0),
            done_commands=data.get("done_commands", 0),
            acknowledged=data.get("acknowledged", False),
            cursor=data.get("cursor", 0),
            order_end=data.get("order_end", 0),
            phase=data.get("phase"),
            wait_started=data.get("wait_started"),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            dispatched_at=data.get("dispatched_at"),
        )


# =============================================================================
# JobStore — 영속화 + 메모리 인덱스
# =============================================================================

class JobStore:
    def __init__(self, data_root: Path | str) -> None:
        self._lock = threading.RLock()
        self._sites_root = Path(data_root) / "sites"
        self._jobs: dict[str, Job] = {}
        self._seq: dict[str, int] = {}   # site_id -> 마지막으로 쓴 번호
        self._load_all()

    # -- 영속화 ----------------------------------------------------------

    def _jobs_path(self, site_id: str) -> Path:
        _check_seg(site_id, "site_id")
        return self._sites_root / site_id / JOBS_FILE

    def _load_all(self) -> None:
        if not self._sites_root.is_dir():
            return
        for site_dir in sorted(p for p in self._sites_root.iterdir() if p.is_dir()):
            path = site_dir / JOBS_FILE
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                log.error("jobs.json 로드 실패 %s: %s", site_dir.name, exc)
                continue
            self._seq[site_dir.name] = int(data.get("seq", 0))
            for job_id, entry in (data.get("jobs") or {}).items():
                try:
                    self._jobs[job_id] = Job.from_dict(job_id, entry)
                except (KeyError, ValueError, JobError) as exc:
                    log.error("job %r 엔트리 무시: %s", job_id, exc)
        if self._jobs:
            log.info("job %d개 로드", len(self._jobs))

    def _persist_site(self, site_id: str) -> None:
        path = self._jobs_path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        jobs = {j.job_id: j.to_dict() for j in self._jobs.values() if j.site_id == site_id}
        payload = {"seq": self._seq.get(site_id, 0), "jobs": jobs}
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def save(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist_site(job.site_id)

    # -- CRUD ----------------------------------------------------------

    def create(
        self, site_id: str, commands: list[dict], robot_id: Optional[str] = None
    ) -> Job:
        _check_seg(site_id, "site_id")
        if not commands:
            raise JobError("job 에 명령이 하나도 없다")
        parsed = [_parse_command(e) for e in commands]
        if robot_id is not None:
            _check_seg(robot_id, "robot_id")
        with self._lock:
            n = self._seq.get(site_id, 0) + 1
            self._seq[site_id] = n
            job_id = f"{site_id}-J{n:04d}"
            job = Job(job_id=job_id, site_id=site_id, commands=parsed, robot_id=robot_id)
            self._jobs[job_id] = job
            self._persist_site(site_id)
        log.info("job 생성: %s (명령 %d개, 로봇 %s)", job_id, len(parsed), robot_id or "-")
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"job {job_id!r} 없음")
        return job

    def list(self, site_id: Optional[str] = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if site_id is not None:
            jobs = [j for j in jobs if j.site_id == site_id]
        return sorted(jobs, key=lambda j: j.job_id)

    def jobs_for_robot(self, robot_id: str, *, status: Optional[JobStatus] = None) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.robot_id == robot_id]
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def delete(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"job {job_id!r} 없음")
            if job.status == JobStatus.WORKING:
                raise JobError(f"job {job_id} 는 수행 중이다 — 먼저 취소해라")
            del self._jobs[job_id]
            self._persist_site(job.site_id)
        log.info("job 삭제: %s", job_id)


# =============================================================================
# Order 컴파일
# =============================================================================

KEEP_THETA_WAIT_SEC = 0.5   # keep_theta 노드에서 정렬 후 잠깐 서 있는 시간


def compile_to_order(job: Job, registry: SiteRegistry) -> Order:
    """
    job 의 move 명령을 `Order` 로 만든다. 모든 목적지는 같은 맵이어야 하고,
    그 맵은 배정 로봇의 배정 맵과 일치해야 한다. 지금은 전부 `released=true`
    (트래픽 제어 붙기 전).
    """
    if job.robot_id is None:
        raise JobError("배정된 로봇이 없다")
    if not job.commands:
        raise JobError("명령이 없다")

    # [homework_iot 추가] cursor 부터 다음 wait 명령 직전까지의 연속 move 구간만 Order 로 만든다.
    end = job.cursor
    while end < len(job.commands) and isinstance(job.commands[end], MoveCommand):
        end += 1
    segment = job.commands[job.cursor:end]
    if not segment:
        raise JobError("Order 로 만들 move 명령이 없다")

    loc_store = registry.location_store(job.site_id)
    resolved = []  # (target, map_id, Pose)
    for cmd in segment:
        if not isinstance(cmd, MoveCommand):
            raise JobError(f"{cmd.kind} 명령은 아직 Order 로 컴파일할 수 없다")
        try:
            map_id, pose = loc_store.resolve_pose(cmd.target)
        except LocationNotFound:
            raise JobError(f"location {cmd.target!r} 없음") from None
        except LocationError as exc:
            raise JobError(str(exc)) from None
        resolved.append((cmd.target, map_id, pose))

    map_ids = {r[1] for r in resolved}
    if len(map_ids) > 1:
        raise JobError(f"한 job 의 목적지가 여러 맵에 걸쳐 있다: {sorted(map_ids)}")
    target_map = map_ids.pop()

    assignment = registry.assignment_for(job.robot_id)
    if assignment is None:
        raise JobError(f"로봇 {job.robot_id} 가 어느 site 에도 등록돼 있지 않다")
    if assignment.site_id != job.site_id:
        raise JobError(f"로봇 {job.robot_id} 는 다른 site({assignment.site_id})에 등록돼 있다")
    if assignment.map_id != target_map:
        raise JobError(
            f"로봇 {job.robot_id} 배정 맵은 {assignment.map_id!r} 인데 "
            f"job 목적지는 {target_map!r} 맵이다"
        )

    order_id = job.order_id or job.job_id
    # [homework_iot 추가] keep_theta location 은 마지막이 아니어도 그 지점에서 멈춰 정렬해야 한다.
    # 로봇(AMR_client executor.replan)은 액션이 없는 released 노드를 한 경로로 이어 달리며 마지막
    # 노드의 theta 만 쓴다. 짧은 WAIT 액션을 붙이면 구간이 거기서 끊겨 theta 정렬 후 다음으로 간다.
    last = len(resolved) - 1
    nodes = []
    for i, (target, _map_id, pose) in enumerate(resolved):
        action = None
        if i < last and loc_store.get(target).keep_theta:
            action = Action(
                action_id=f"{order_id}-a{i}",
                action_type=ActionType.WAIT,
                duration=KEEP_THETA_WAIT_SEC,
            )
        nodes.append(OrderNode(
            node_id=f"{order_id}-n{i}",
            sequence_id=i,
            released=True,
            position=pose,
            action=action,
        ))
    return Order(
        header_id=0,
        timestamp=time.time(),
        robot_id=job.robot_id,
        order_id=order_id,
        order_update_id=job.order_update_id or 1,
        task_id=job.job_id,
        nodes=nodes,
    )


# =============================================================================
# JobCoordinator — 디스패치 + 로봇 보고로 상태 되짚기
# =============================================================================

class JobCoordinator:
    """
    job 을 로봇에 내려보내고(`dispatch`), 로봇 state 를 보고 job 상태를 옮긴다.
    fleet 리스너는 paho 스레드에서 불린다 — 여기서 하는 건 메모리 갱신 + 파일
    쓰기뿐이라 블로킹은 없다.
    """

    def __init__(
        self,
        fleet: Fleet,
        registry: SiteRegistry,
        store: JobStore,
        mqtt: FmsMqttClient,
        flags: Optional[FlagStore] = None,
        io: Optional[PiIoClient] = None,
    ) -> None:
        self._io = io
        self._io_running: set[str] = set()   # set_output 요청 중인 job_id
        self._cond_since: dict[str, float] = {}   # job_id -> wait_flag 조건이 처음 참이 된 시각 (hold 용, 메모리만)
        self._flags = flags if flags is not None else FlagStore()
        self._fleet = fleet
        self._registry = registry
        self._store = store
        self._mqtt = mqtt
        self._unsubscribe = None
        self._lock = threading.RLock()   # dispatch(API 스레드) 와 자동 배정(paho 스레드) 직렬화

    # -- 수명 주기 ----------------------------------------------------

    def start(self) -> None:
        self._unsubscribe = self._fleet.add_listener(self._on_fleet_event)
        log.info("job 코디네이터 시작")

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # -- 디스패치 (API 핸들러) --------------------------------------

    def dispatch(self, job_id: str, robot_id: Optional[str] = None) -> Job:
        with self._lock:
            return self._dispatch(job_id, robot_id)

    def _dispatch(self, job_id: str, robot_id: Optional[str] = None) -> Job:
        job = self._store.get(job_id)
        if job.status != JobStatus.PENDING:
            raise JobError(f"job {job_id} 는 {job.status.value} 상태다 — pending 만 디스패치 가능")

        target_robot = robot_id or job.robot_id
        if not target_robot:
            raise JobError("배정할 로봇이 없다 (robot_id 를 줘라)")

        record = self._fleet.get(target_robot)
        if record is None or record.state is None:
            raise JobError(f"로봇 {target_robot} 의 state 를 아직 못 받았다")
        if not record.available_for_job(self._fleet.stale_after):
            raise JobError(
                f"로봇 {target_robot} 가 작업 수락 불가 상태다 ({record.state.display_state})"
            )

        assignment = self._registry.assignment_for(target_robot)
        if assignment is None or assignment.site_id != job.site_id:
            raise JobError(f"로봇 {target_robot} 가 site {job.site_id} 에 등록돼 있지 않다")

        # 맵 버전 게이팅 — 오래된 금지구역 들고 도는 로봇에 job 안 준다
        try:
            map_store = self._registry.map_store(assignment.site_id)
            active_version = map_store.active_version(assignment.map_id)
        except Exception:
            active_version = None
        if not active_version:
            raise JobError(f"배정 맵 {assignment.map_id} 에 활성 버전이 없다")
        if record.state.map_version != active_version:
            raise JobError(
                f"로봇 {target_robot} 맵 버전({record.state.map_version})이 "
                f"배정 맵 활성 버전({active_version})과 다르다 — SET_MAP 동기화 대기"
            )

        job.robot_id = target_robot
        job.cursor = 0
        job.done_commands = 0
        job.error = None

        self._begin_step(job)  # JobError 던질 수 있음 (status 는 아직 pending)
        job.status = JobStatus.WORKING
        job.dispatched_at = _now_iso()
        job.touch()
        self._store.save(job)
        log.info("job 디스패치: %s -> %s (%s)", job_id, target_robot, job.phase)
        return job

    def _begin_step(self, job: Job) -> None:
        """[homework_iot 추가] cursor 의 명령을 시작한다. move 구간이면 Order 발행, wait 면 대기 시작."""
        if job.cursor >= len(job.commands):
            job.done_commands = len(job.commands)
            job.phase = None
            self._finish(job, JobStatus.SUCCESS, None)
            return
        cmd = job.commands[job.cursor]
        if isinstance(cmd, MoveCommand):
            end = job.cursor
            while end < len(job.commands) and isinstance(job.commands[end], MoveCommand):
                end += 1
            job.order_id = job.job_id if job.cursor == 0 else f"{job.job_id}~c{job.cursor}"
            job.order_update_id = 1
            job.order_end = end
            order = compile_to_order(job, self._registry)
            self._mqtt.publish_order(order)
            job.phase = "order"
        elif isinstance(cmd, SetOutputCommand):
            job.order_id = None
            job.phase = "io"
        else:
            job.order_id = None
            job.phase = "wait"
            job.wait_started = time.time()
            self._cond_since.pop(job.job_id, None)
        job.acknowledged = False
        job.done_commands = job.cursor
        job.touch()
        self._store.save(job)
        if job.phase == "io":
            self._start_io(job)

    def _start_io(self, job: Job) -> None:
        """파이 HTTP 호출은 최대 수 초 걸릴 수 있어 MQTT(paho) 스레드를 막지 않도록 별도 스레드에서 한다."""
        if job.job_id in self._io_running:
            return
        self._io_running.add(job.job_id)
        threading.Thread(
            target=self._run_io, args=(job.job_id, job.cursor), name=f"io-{job.job_id}", daemon=True
        ).start()

    def _run_io(self, job_id: str, cursor: int) -> None:
        error = None
        try:
            job = self._store.get(job_id)
            cmd = job.commands[cursor]
            if self._io is None:
                raise IoError("라즈베리파이 클라이언트가 설정되지 않았다")
            self._io.set_output(cmd.device, cmd.value)
        except IoError as exc:
            error = str(exc)
        except Exception as exc:  # 예상 못 한 오류도 job 을 영원히 멈춰 두지 않는다
            error = f"set_output 실패: {exc}"
        with self._lock:
            self._io_running.discard(job_id)
            job = self._store.get(job_id)
            if job.status != JobStatus.WORKING or job.phase != "io" or job.cursor != cursor:
                return   # 그 사이 취소/재시작됨
            if error:
                job.phase = None
                self._finish(job, JobStatus.FAILED, error)
                return
            log.info("job %s: set_output %s=%s 완료", job_id, job.commands[cursor].device, job.commands[cursor].value)
            job.cursor += 1
            self._next_step(job)

    def _next_step(self, job: Job) -> None:
        try:
            self._begin_step(job)
        except JobError as exc:
            self._finish(job, JobStatus.FAILED, str(exc))

    def _advance_wait(self, job: Job) -> None:
        cmd = job.commands[job.cursor]
        if isinstance(cmd, WaitCommand):
            done = time.time() - (job.wait_started or 0.0) >= cmd.seconds
        else:
            done = self._flag_condition_met(job, cmd)
            if not done and cmd.timeout is not None and time.time() - (job.wait_started or 0.0) >= cmd.timeout:
                if cmd.on_timeout == "fail":
                    self._cond_since.pop(job.job_id, None)
                    job.phase = None
                    self._finish(job, JobStatus.FAILED, f"플래그 {cmd.flag} 대기 시간 초과 ({cmd.timeout:g}초)")
                    return
                done = True   # on_timeout == "continue"
        if done:
            self._cond_since.pop(job.job_id, None)
            job.cursor += 1
            self._next_step(job)

    def _flag_condition_met(self, job: Job, cmd: "WaitFlagCommand") -> bool:
        """신호가 원하는 값이고(미수신/만료는 불충족) hold 초 동안 끊김 없이 유지됐나."""
        now = time.time()
        entry = self._flags.entry(cmd.flag)
        ok = (entry is not None and now - entry[1] <= FLAG_STALE_SEC
              and entry[0] is (cmd.until == "raised"))
        if not ok:
            self._cond_since.pop(job.job_id, None)
            return False
        since = self._cond_since.setdefault(job.job_id, now)
        return now - since >= cmd.hold

    def cancel(self, job_id: str) -> Job:
        job = self._store.get(job_id)
        if job.status.terminal:
            raise JobError(f"job {job_id} 는 이미 종료됐다 ({job.status.value})")

        if job.status == JobStatus.WORKING and job.robot_id and job.phase == "order":
            action = InstantAction(
                header_id=0,
                timestamp=time.time(),
                robot_id=job.robot_id,
                action_id=f"CANCEL-{int(time.time() * 1000)}-{job.robot_id}",
                action_type=InstantActionType.CANCEL_ORDER,
                params={},
            )
            self._mqtt.publish_instant(action)

        job.status = JobStatus.CANCELED
        job.touch()
        self._store.save(job)
        log.info("job 취소: %s", job_id)
        return job

    # -- fleet 이벤트 (paho 스레드) --------------------------------

    def _on_fleet_event(self, event: FleetEvent) -> None:
        if event.kind != "state":
            return
        with self._lock:
            working = self._store.jobs_for_robot(event.robot_id, status=JobStatus.WORKING)
            for job in working:
                self._advance(job, event.record.state)
            self._assign_next(event.robot_id)

    def _assign_next(self, robot_id: str) -> None:
        """
        [homework_iot 추가] 큐 방식 자동 배정. 로봇이 놀고 있으면 pending job 을 먼저 만든 순서로
        훑어, 이 로봇 지정 job 이거나 로봇 미지정(아무 로봇이나) job 중 첫 번째를 디스패치한다.
        지정 로봇이 바쁜 job 은 건너뛰고 뒤의 미지정 job 을 먼저 줄 수 있다.
        """
        if self._store.jobs_for_robot(robot_id, status=JobStatus.WORKING):
            return
        assignment = self._registry.assignment_for(robot_id)
        if assignment is None:
            return
        for job in self._store.list(assignment.site_id):
            if job.status != JobStatus.PENDING or job.robot_id not in (None, robot_id):
                continue
            try:
                self._dispatch(job.job_id, robot_id)
                return
            except JobError as exc:
                # 로봇 상태/맵 문제는 다음 state(200ms)에 다시 시도된다. 로그 폭주 방지로 debug.
                log.debug("자동 배정 보류 %s -> %s: %s", job.job_id, robot_id, exc)
                if "작업 수락 불가" in str(exc) or "맵 버전" in str(exc):
                    return   # 로봇 쪽 사유면 다른 job 도 마찬가지

    def _advance(self, job: Job, state: Optional[State]) -> None:
        if state is None:
            return

        fatal = [e for e in state.errors if e.level == ErrorLevel.FATAL]
        if fatal:
            self._finish(job, JobStatus.FAILED, "; ".join(e.error_type for e in fatal))
            return

        if job.phase == "wait":
            self._advance_wait(job)
            return
        if job.phase == "io":
            self._start_io(job)   # 이미 요청 중이면 무시. 서버 재시작 뒤 복구도 여기서 (set_output 은 멱등)
            return

        if state.order_id == job.order_id:
            # 로봇이 우리 오더를 물었다. 진행률 갱신.
            job.acknowledged = True
            done = job.cursor + min(state.last_node_sequence_id + 1, job.order_end - job.cursor)
            if done != job.done_commands:
                job.done_commands = done
                job.touch()
                self._store.save(job)
            return

        if job.acknowledged:
            # 우리 order_id 를 봤었는데 지금은 비었다/딴 오더다.
            if state.state == RobotState.ERROR:
                self._finish(job, JobStatus.FAILED, "로봇이 ERROR 상태")
            else:
                job.cursor = job.order_end    # 이 구간 끝 -> 다음 명령(wait 또는 다음 move 구간)
                self._next_step(job)

    def _finish(self, job: Job, status: JobStatus, error: Optional[str]) -> None:
        job.status = status
        job.error = error
        job.touch()
        self._store.save(job)
        log.info("job %s -> %s%s", job.job_id, status.value, f" ({error})" if error else "")
