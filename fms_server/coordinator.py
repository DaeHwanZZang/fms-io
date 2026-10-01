"""
Site 코디네이터 — 로봇을 배정 맵과 동기화시킨다
==============================================

책임 하나: **등록된 로봇이 배정 맵의 활성 버전을 쓰도록 `SET_MAP` 을 보낸다.**

- 로봇 state 수신 시: 등록된 로봇이면, 보고한 `map_version` 을 배정 맵의 활성
  버전과 비교. 다르면 `SET_MAP` 발행
- 관리자가 맵을 activate 하거나 로봇을 (재)배정하면 즉시 `SET_MAP` 발행
- 미등록 로봇은 건드리지 않는다 (job 도 SET_MAP 도 없음 — 관측만)

과도한 발행을 막으려고 로봇별 쿨다운을 둔다: 같은 목표 버전으로는
`resend_cooldown` 초 안에 다시 안 보낸다 (목표 버전이 바뀌면 즉시 보낸다).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from common.schemas import InstantAction, InstantActionType
from fms_server.fleet import Fleet, FleetEvent
from fms_server.mqtt_client import FmsMqttClient
from fms_server.sites import SiteRegistry

log = logging.getLogger("fms.coordinator")


class SiteCoordinator:
    def __init__(
        self,
        fleet: Fleet,
        registry: SiteRegistry,
        mqtt: FmsMqttClient,
        public_url: str,
        *,
        resend_cooldown: float = 10.0,
    ) -> None:
        self._fleet = fleet
        self._registry = registry
        self._mqtt = mqtt
        self._public_url = public_url.rstrip("/")
        self._resend_cooldown = resend_cooldown

        self._lock = threading.Lock()
        # robot_id -> (target_version, sent_at)
        self._last_sent: dict[str, tuple[str, float]] = {}
        self._unsubscribe = None

    # -- 수명 주기 --------------------------------------------------

    def start(self) -> None:
        self._unsubscribe = self._fleet.add_listener(self._on_fleet_event)
        log.info("코디네이터 시작 (SET_MAP 재발행 쿨다운 %.0fs)", self._resend_cooldown)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # -- fleet 이벤트 (paho 스레드) --------------------------------

    def _on_fleet_event(self, event: FleetEvent) -> None:
        if event.kind != "state":
            return
        record = event.record
        reported = record.state.map_version if record.state else None
        self._sync_robot(event.robot_id, reported_version=reported)

    # -- 외부 트리거 (API 핸들러) --------------------------------

    def notify_robot(self, robot_id: str) -> bool:
        """로봇 하나를 배정 맵 활성 버전으로 동기화. 발행했으면 True."""
        return self._sync_robot(robot_id, reported_version=None, force=True)

    def notify_map_activated(self, site_id: str, map_id: str) -> list[str]:
        """이 맵에 배정된 (그 site 의) 모든 로봇에 SET_MAP. 발행 대상 목록 반환."""
        try:
            site = self._registry.get_site(site_id)
        except Exception:
            return []
        sent: list[str] = []
        for robot_id, assignment in list(site.robots.items()):
            if assignment.map_id == map_id and self._sync_robot(
                robot_id, reported_version=None, force=True
            ):
                sent.append(robot_id)
        return sent

    # -- 핵심 -----------------------------------------------------

    def _sync_robot(
        self, robot_id: str, *, reported_version: Optional[str], force: bool = False
    ) -> bool:
        assignment = self._registry.assignment_for(robot_id)
        if assignment is None:
            return False  # 미등록 — 관측만

        try:
            store = self._registry.map_store(assignment.site_id)
            target_version = store.active_version(assignment.map_id)
            map_uid = store.map_uid(assignment.map_id)
        except Exception as exc:
            log.debug("동기화 스킵 %s: %s", robot_id, exc)
            return False
        if not target_version:
            return False  # 배정 맵에 활성 버전이 아직 없음

        if reported_version == target_version and not force:
            return False  # 이미 최신

        with self._lock:
            last = self._last_sent.get(robot_id)
            now = time.time()
            if (
                last is not None
                and last[0] == target_version
                and (now - last[1]) < self._resend_cooldown
                and not force
            ):
                return False
            self._last_sent[robot_id] = (target_version, now)

        # URL 은 관리자 map_id 로 (사람이 읽는 라우트). 로봇이 보는 이름은 map_uid.
        url = (
            f"{self._public_url}/api/v1/sites/{assignment.site_id}"
            f"/maps/{assignment.map_id}/bundle?version={target_version}"
        )
        action = InstantAction(
            header_id=0,
            timestamp=time.time(),
            robot_id=robot_id,
            action_id=f"SETMAP-{int(time.time() * 1000)}-{robot_id}",
            action_type=InstantActionType.SET_MAP,
            params={
                "map_id": map_uid,          # 로봇은 이 값을 맵 이름으로 쓴다
                "map_version": target_version,
                "url": url,
            },
        )
        self._mqtt.publish_instant(action)
        log.info(
            "SET_MAP -> %s (site %s / 맵 %s [uid %s] / 버전 %s, 로봇 보고=%s)",
            robot_id, assignment.site_id, assignment.map_id, map_uid, target_version, reported_version,
        )
        return True
