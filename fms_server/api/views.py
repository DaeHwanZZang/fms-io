"""REST/WebSocket 응답 DTO.

`Fleet` 의 `RobotRecord` (내부 표현)를 API 바깥으로 내보낼 모양으로 옮긴다.
로봇이 보고한 `State` 를 그대로 노출하되, 파생값(liveness, 등록 여부, 맵 동기화
상태)을 얹는다.

등록 정보(site/맵 배정)는 `SiteRegistry` 가 갖고 있고 Fleet 은 모른다. 그래서
`RobotView.from_record` 에 assignment 를 선택적으로 넘긴다.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from common.schemas import State
from fms_server.fleet import Fleet, RobotLiveness, RobotRecord
from fms_server.sites import RobotAssignment, SiteRegistry


class RobotView(BaseModel):
    robot_id: str
    liveness: RobotLiveness
    available_for_job: bool

    # 등록 정보 (미등록이면 None)
    registered: bool = False
    site_id: Optional[str] = None
    assigned_map_id: Optional[str] = None       # 관리자 라벨 ("floor-1")
    assigned_map_uid: Optional[str] = None       # FMS 전역 id (로봇이 보는 이름)
    # 배정 맵의 활성 버전과 로봇이 보고한 버전이 일치하는가
    map_synced: bool = False

    connection: Optional[str] = None
    first_seen: float
    last_state_at: Optional[float] = None
    last_connection_at: Optional[float] = None

    state: Optional[State] = None

    @classmethod
    def from_record(
        cls,
        record: RobotRecord,
        stale_after: float,
        *,
        assignment: Optional[RobotAssignment] = None,
        active_map_version: Optional[str] = None,
        assigned_map_uid: Optional[str] = None,
    ) -> "RobotView":
        base_liveness = record.liveness(stale_after)
        reported_version = record.state.map_version if record.state else None

        if assignment is None:
            liveness = RobotLiveness.UNREGISTERED
            # 접속조차 안 한 건 아니니 base 가 OFFLINE/LOST 면 그걸 우선.
            if base_liveness in (RobotLiveness.OFFLINE, RobotLiveness.LOST):
                liveness = base_liveness
            map_synced = False
            available = False
        else:
            liveness = base_liveness
            map_synced = (
                active_map_version is not None
                and reported_version == active_map_version
            )
            available = record.available_for_job(stale_after) and map_synced

        return cls(
            robot_id=record.robot_id,
            liveness=liveness,
            available_for_job=available,
            registered=assignment is not None,
            site_id=assignment.site_id if assignment else None,
            assigned_map_id=assignment.map_id if assignment else None,
            assigned_map_uid=assigned_map_uid,
            map_synced=map_synced,
            connection=record.connection.value if record.connection is not None else None,
            first_seen=record.first_seen,
            last_state_at=record.last_state_at,
            last_connection_at=record.last_connection_at,
            state=record.state,
        )


def build_robot_view(record: RobotRecord, fleet: Fleet, registry: SiteRegistry) -> RobotView:
    """RobotRecord + 등록 정보(site/맵 배정, 배정 맵 활성 버전·uid) → RobotView.

    robots API, WebSocket 브로드캐스터, FleetView 가 전부 이걸 쓴다.
    """
    assignment = registry.assignment_for(record.robot_id)
    active_version = None
    map_uid = None
    if assignment is not None:
        try:
            store = registry.map_store(assignment.site_id)
            active_version = store.active_version(assignment.map_id)
            map_uid = store.map_uid(assignment.map_id)
        except Exception:
            active_version = None
            map_uid = None
    return RobotView.from_record(
        record,
        fleet.stale_after,
        assignment=assignment,
        active_map_version=active_version,
        assigned_map_uid=map_uid,
    )


class FleetView(BaseModel):
    robots: list[RobotView]
    count: int
    available_count: int
    unregistered_count: int

    @classmethod
    def from_fleet(cls, fleet: Fleet, registry: SiteRegistry) -> "FleetView":
        views = [build_robot_view(r, fleet, registry) for r in fleet.all()]
        return cls(
            robots=views,
            count=len(views),
            available_count=sum(1 for v in views if v.available_for_job),
            unregistered_count=sum(1 for v in views if not v.registered),
        )
