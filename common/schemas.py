"""
FMS <-> Robot Client 통신 메시지 스키마
=======================================

이 파일은 FMS 서버와 로봇 클라이언트가 공유하는 '계약서'다.
양쪽 모두 이 파일을 import 해서 사용한다.

    common/
      schemas.py   <- 이 파일
    fms_server/
    robot_client/

VDA5050 규격을 참고하여 단순화했다.

MQTT 토픽
---------
    fms/v1/{robot_id}/connection   Robot -> FMS   접속 상태 (LWT 사용, retain)
    fms/v1/{robot_id}/state        Robot -> FMS   주기 상태 보고 (200ms)
    fms/v1/{robot_id}/order        FMS -> Robot   작업 지시서
    fms/v1/{robot_id}/instant      FMS -> Robot   즉시 명령 (긴급정지 등)

Pydantic v2 기준.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "1.0"


# =============================================================================
# 공통 베이스
# =============================================================================

class FmsMessage(BaseModel):
    """모든 메시지의 공통 헤더."""

    model_config = ConfigDict(
        extra="forbid",        # 정의되지 않은 필드가 오면 에러 -> 오타 조기 발견
        use_enum_values=False,
        validate_assignment=True,
    )

    version: str = PROTOCOL_VERSION
    header_id: int = Field(ge=0, description="송신자별 단조 증가 카운터. 유실/순서 검출용")
    timestamp: float = Field(description="시뮬레이션 시각(초). 실시간 모드면 unix time")
    robot_id: str = Field(min_length=1, description="예: AMR-001")


# =============================================================================
# Enum 정의
# =============================================================================

class RobotState(str, Enum):
    """
    로봇 상태머신. 실물 로봇 인터페이스(start/stop/emergency 버튼, auto/manual
    토글)를 기준으로 정의했다. 다섯 값은 서로 배타적이다.

        IDLE       대기. job 수신 가능
        BUSY       job 수행 중. 세부 단계는 State.sub_state 참조
        CHARGING   충전 중
        ERROR      오류. 사람 개입 필요 (start 버튼 / RESET_ERROR 로 해제)
        EMERGENCY  물리 e-stop 래치가 눌림. 래치를 다시 눌러야만 풀린다

    EMERGENCY 는 다른 상태를 **덮어써서 보고하는 값**이다. 로봇 내부에서는
    base state(IDLE/BUSY/CHARGING/ERROR)를 그대로 들고 있다가 래치가 풀리면
    즉시 그 상태로 돌아간다 — 진행 중이던 job 도 유지된다 (PAUSE 와 동일한 동작).
    그래서 EMERGENCY 는 errors[] 에 FATAL 로 들어가지 않는다. FATAL 로 넣으면
    "FATAL 이면 오더를 버린다" 규칙을 타서 job 이 사라진다.

    OFFLINE 은 여기 없다. 전원이 꺼진 로봇은 자기가 OFFLINE 이라고 발행할 수
    없기 때문이다 — connection 토픽의 ConnectionState 로 브로커가 대신 알린다.
    """

    IDLE = "IDLE"
    BUSY = "BUSY"
    CHARGING = "CHARGING"
    ERROR = "ERROR"
    EMERGENCY = "EMERGENCY"


class BusySubState(str, Enum):
    """
    BUSY 의 세부 단계. state == BUSY 일 때만 값이 있다.

    WAITING 을 BUSY 안에 묻지 않고 따로 남긴 이유: 트래픽 제어의 데드락 감지는
    "A 가 B 를 기다리고 B 가 A 를 기다린다" 를 봐야 하는데, BUSY 만 봐서는
    주행 중인지 통행권 대기인지 구분할 수 없다.

    실제로 움직이고 있는지 여부와는 별개다. paused / mode==MANUAL 로 정지해
    있어도 sub_state 는 그 job 이 어느 단계에 있는지를 계속 가리킨다.
    """

    MOVING = "MOVING"            # 노드 간 이동
    WAITING = "WAITING"          # released=false 노드 앞에서 통행권 대기
    ACTING = "ACTING"            # PICK/DROP 수행 중 (CHARGE 는 CHARGING 상태로 빠진다)


class OperatingMode(str, Enum):
    """
    로봇 패널의 auto/manual 토글 스위치.

    MANUAL 이면 신규 job 을 받지 않고, 진행 중이던 job 은 일시정지한다
    (사람이 조이스틱으로 몰고 가는 상황). AUTO 로 되돌리면 현재 위치 기준으로
    경로를 다시 짜서 이어서 수행한다.
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"


class PauseSource(str, Enum):
    """
    일시정지를 건 주체. 사람이 손으로 세운 로봇을 원격에서 푸는 걸 막는다.

        LOCAL  로봇 패널 start 버튼 / 로컬 제어판. FMS 는 이걸 풀 수 없다
        FMS    FMS 의 PAUSE 즉시명령. FMS/로컬 양쪽 다 풀 수 있다
    """

    LOCAL = "LOCAL"
    FMS = "FMS"


class ActionType(str, Enum):
    """노드에서 수행할 동작."""

    PICK = "PICK"
    DROP = "DROP"
    CHARGE = "CHARGE"
    WAIT = "WAIT"


class ActionStatus(str, Enum):
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


class ErrorLevel(str, Enum):
    WARNING = "WARNING"   # 작업 계속 가능
    FATAL = "FATAL"       # 작업 중단, 재배정 필요


class ConnectionState(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"              # 정상 종료
    CONNECTION_BROKEN = "CONNECTION_BROKEN"  # LWT 로 브로커가 대신 발행


class InstantActionType(str, Enum):
    """
    FMS 가 보내는 즉시 명령.

    물리 e-stop(EMERGENCY 상태)은 여기 없다 — 로봇 몸체의 래치 버튼이라 원격에서
    누를 수도, 풀 수도 없다. FMS 가 할 수 있는 원격 정지는 PAUSE 뿐이다.
    """

    CANCEL_ORDER = "CANCEL_ORDER"
    PAUSE = "PAUSE"                  # 원격 일시정지 (pause_source=FMS). job 은 유지된다
    RESUME = "RESUME"                # FMS 가 건 pause 만 풀 수 있다 (LOCAL 은 못 푼다)
    RESET_ERROR = "RESET_ERROR"
    SET_MAP = "SET_MAP"             # "이 맵으로 갈아타라". params: map_id, map_version, url
                                    #   map_id = FMS 전역 고유 id (map_uid). 로봇은 이걸
                                    #   맵 이름으로 삼는다 (다수 로봇이 이름만 보고 동일 맵 판별).
                                    #   url 로 HTTP GET 해서 맵 번들을 받는다 (MQTT 로는 안 보낸다).
                                    #   로봇은 manifest sha 를 비교해 이미 최신이면 다운로드를 건너뛴다.
                                    #   동기화가 끝나면 State.map_id(=map_uid)/map_version 을 실어 보고한다.
    INJECT_FAULT = "INJECT_FAULT"    # 시뮬레이터 전용: 장애 주입
    INJECT_OBSTACLE = "INJECT_OBSTACLE"  # 개발용: 동적 장애물 수동 배치 (tools/place_obstacle.py)
    CLEAR_OBSTACLE = "CLEAR_OBSTACLE"    # 개발용: 수동 배치 장애물 제거 (obstacle_id 없으면 전체)
    START_OBSTACLE_ZONE = "START_OBSTACLE_ZONE"  # 개발용: 구역 통과형 장애물 스폰 시작/재설정 (tools/obstacle_zone.py)
    STOP_OBSTACLE_ZONE = "STOP_OBSTACLE_ZONE"    # 개발용: 신규 스폰 중단 (이미 나온 장애물은 유지)


# =============================================================================
# 값 객체
# =============================================================================

class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    theta: float = Field(ge=-3.15, le=3.15, description="라디안, -pi ~ pi")


class Action(BaseModel):
    """노드에 도착한 뒤 수행할 동작."""

    model_config = ConfigDict(extra="forbid")

    action_id: str
    action_type: ActionType
    duration: float = Field(default=0.0, ge=0, description="예상 소요 시간(초)")
    payload_id: Optional[str] = Field(default=None, description="적재물 식별자")


class TimeWindow(BaseModel):
    """트래픽 제어용 시공간 예약 구간."""

    model_config = ConfigDict(extra="forbid")

    enter: float = Field(description="진입 예정 시각(초)")
    exit: float = Field(description="이탈 예정 시각(초)")

    @model_validator(mode="after")
    def _check_order(self) -> "TimeWindow":
        if self.exit < self.enter:
            raise ValueError("exit 은 enter 보다 빠를 수 없다")
        return self


# =============================================================================
# Order : FMS -> Robot
# =============================================================================

class OrderNode(BaseModel):
    """오더를 구성하는 경유 노드 하나."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    sequence_id: int = Field(ge=0, description="오더 내 순서. 0부터 증가")
    released: bool = Field(
        description=(
            "통행 허가 여부. false 면 로봇은 이 노드 직전에서 정지하고 "
            "WAITING 상태로 전환한다. 트래픽 제어의 실행 수단."
        )
    )
    position: Pose
    action: Optional[Action] = None
    reserved_window: Optional[TimeWindow] = None


class Order(FmsMessage):
    """작업 지시서. 로봇은 이 노드 목록을 순서대로 처리한다."""

    order_id: str
    order_update_id: int = Field(
        default=0,
        ge=0,
        description=(
            "같은 order_id 로 노드를 추가하거나 released 를 갱신할 때 증가. "
            "로봇은 자신이 가진 값보다 큰 것만 수용한다."
        ),
    )
    task_id: Optional[str] = Field(default=None, description="상위 작업(WMS) 식별자")
    nodes: list[OrderNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_sequence(self) -> "Order":
        seqs = [n.sequence_id for n in self.nodes]
        if seqs != sorted(seqs):
            raise ValueError("nodes 는 sequence_id 오름차순이어야 한다")
        if len(set(seqs)) != len(seqs):
            raise ValueError("sequence_id 가 중복되었다")
        # released=false 이후에 다시 true 가 나오면 안 된다 (해제는 앞에서부터 연속)
        seen_false = False
        for n in self.nodes:
            if not n.released:
                seen_false = True
            elif seen_false:
                raise ValueError("released 는 앞에서부터 연속으로 true 여야 한다")
        return self


# =============================================================================
# State : Robot -> FMS
# =============================================================================

class Point(BaseModel):
    """방향 없는 좌표 하나. 계획된 경로의 경유점처럼 각도가 의미 없는 곳에 쓴다."""

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class ObstacleInfo(BaseModel):
    """
    로봇이 센서로 감지한 동적 장애물 하나. 축 정렬 사각형(x, y 중심 + width, height)만
    표현한다 — 회전은 아직 없다. 로봇이 실제로 감지 범위 안에서 본 것만 보고한다
    (전역 장애물 목록이 아니다. robot_client/sim/dynamic_obstacles.py 참고).
    """

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class NodeProgress(BaseModel):
    """현재 오더에서 아직 남은 노드 정보."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    sequence_id: int
    released: bool


class ActionProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    status: ActionStatus


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_type: str = Field(description="예: MOTOR_FAULT, BATTERY_CRITICAL")
    level: ErrorLevel
    description: str = ""


class BatteryInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge: float = Field(ge=0, le=100, description="잔량 %")
    charging: bool = False
    estimated_runtime: Optional[float] = Field(
        default=None, ge=0, description="현재 소모율 기준 잔여 가동 시간(초)"
    )


class State(FmsMessage):
    """로봇 주기 상태 보고. FMS 가 아는 로봇의 유일한 진실."""

    state: RobotState
    sub_state: Optional[BusySubState] = Field(
        default=None, description="state == BUSY 일 때만 값이 있다. 그 외에는 반드시 None"
    )
    mode: OperatingMode = Field(
        default=OperatingMode.AUTO, description="auto/manual 토글. MANUAL 이면 job 을 받지 않는다"
    )
    paused: bool = Field(default=False, description="일시정지 여부. job 은 유지된다")
    pause_source: Optional[PauseSource] = Field(
        default=None, description="일시정지를 건 주체. paused 가 True 일 때만 값이 있다"
    )

    pose: Pose
    velocity: float = Field(default=0.0, description="현재 속도 m/s")

    width: float = Field(default=0.5, gt=0, description="로봇 폭 (m). 뷰어가 실제 크기로 그릴 때 씀")
    length: float = Field(default=0.7, gt=0, description="로봇 길이 (m)")

    map_id: Optional[str] = Field(
        default=None, description="로봇이 현재 주행에 쓰는 맵 ID. FMS 가 매긴 전역 고유 id "
                                  "(map_uid) — SET_MAP 의 map_id params 로 받은 값을 그대로 "
                                  "돌려보낸다. 로봇은 이 이름만 보고 맵을 판별한다"
    )
    map_version: Optional[str] = Field(
        default=None, description="그 맵의 버전 (번들 콘텐츠 해시). map_id 가 있으면 함께 있어야 한다. "
                                  "FMS 는 이 값이 배정 맵 활성 버전과 같은 로봇에게만 job 을 준다"
    )

    local_path: list[Point] = Field(
        default_factory=list,
        description="목적지까지 실제로 밟을 남은 경유점들 (장애물 회피 경로 포함). "
                     "로봇이 스스로 계획한 값이며 FMS 가 준 원본 오더 노드와 다를 수 있다",
    )

    last_node_id: str = Field(description="마지막으로 통과한 노드")
    last_node_sequence_id: int = Field(default=0, ge=0)

    order_id: Optional[str] = None
    order_update_id: int = Field(default=0, ge=0)
    node_states: list[NodeProgress] = Field(
        default_factory=list, description="아직 도달하지 않은 노드들"
    )
    action_states: list[ActionProgress] = Field(default_factory=list)

    battery: BatteryInfo
    errors: list[ErrorInfo] = Field(default_factory=list)

    detected_obstacles: list[ObstacleInfo] = Field(
        default_factory=list,
        description="현재 센서 감지범위 안의 동적 장애물만. 로봇마다 독립적으로 감지하므로 "
                    "같은 물리적 장애물이라도 로봇마다 다르게(또는 아예 안) 보일 수 있다",
    )

    @model_validator(mode="after")
    def _check_state_consistency(self) -> "State":
        if self.state == RobotState.BUSY and self.sub_state is None:
            raise ValueError("state 가 BUSY 면 sub_state 가 있어야 한다")
        if self.state != RobotState.BUSY and self.sub_state is not None:
            raise ValueError("sub_state 는 state == BUSY 일 때만 값을 가진다")
        if self.paused != (self.pause_source is not None):
            raise ValueError("paused 와 pause_source 는 함께 설정돼야 한다")
        if (self.map_id is None) != (self.map_version is None):
            raise ValueError("map_id 와 map_version 은 함께 설정돼야 한다")
        return self

    @property
    def display_state(self) -> str:
        """
        사람이 읽을 상태 한 줄. 뷰어와 로봇 제어판이 같은 문자열을 쓰도록 여기 둔다.
        축이 넷이라 값 하나만 봐서는 상황을 알 수 없다.

            BUSY/MOVING
            BUSY/WAITING (PAUSED:LOCAL)
            EMERGENCY
            IDLE [MANUAL]
        """
        label = self.state.value
        if self.sub_state is not None:
            label = f"{label}/{self.sub_state.value}"
        if self.paused and self.pause_source is not None:
            label += f" (PAUSED:{self.pause_source.value})"
        if self.mode == OperatingMode.MANUAL:
            label += " [MANUAL]"
        return label

    @property
    def is_available(self) -> bool:
        """
        새 작업을 받을 수 있는 상태인가. FMS 할당기는 이 술어만 본다.

        접속 여부(ConnectionState)는 다른 토픽이라 여기 없다 — FMS 가 이 값과
        connection 을 함께 보고 최종 판단한다.
        """
        return (
            self.state == RobotState.IDLE
            and self.mode == OperatingMode.AUTO
            and not self.paused
            and self.order_id is None
            and not any(e.level == ErrorLevel.FATAL for e in self.errors)
        )


# =============================================================================
# Connection : Robot -> FMS  (MQTT LWT, retain=True)
# =============================================================================

class Connection(FmsMessage):
    """
    접속 상태. 로봇이 붙을 때 ONLINE 을 발행하고,
    브로커에 CONNECTION_BROKEN 을 LWT 로 등록해 둔다.
    프로세스가 강제 종료되면 브로커가 대신 발행한다.
    """

    connection_state: ConnectionState


# =============================================================================
# InstantAction : FMS -> Robot
# =============================================================================

class InstantAction(FmsMessage):
    """오더와 무관하게 즉시 처리해야 하는 명령."""

    action_id: str
    action_type: InstantActionType
    params: dict[str, float | str | bool] = Field(default_factory=dict)


# =============================================================================
# 토픽 헬퍼
# =============================================================================

TOPIC_PREFIX = "fms/v1"


def topic_order(robot_id: str) -> str:
    return f"{TOPIC_PREFIX}/{robot_id}/order"


def topic_state(robot_id: str) -> str:
    return f"{TOPIC_PREFIX}/{robot_id}/state"


def topic_connection(robot_id: str) -> str:
    return f"{TOPIC_PREFIX}/{robot_id}/connection"


def topic_instant(robot_id: str) -> str:
    return f"{TOPIC_PREFIX}/{robot_id}/instant"


TOPIC_STATE_ALL = f"{TOPIC_PREFIX}/+/state"
TOPIC_CONNECTION_ALL = f"{TOPIC_PREFIX}/+/connection"
