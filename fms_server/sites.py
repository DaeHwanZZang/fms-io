"""
Site 레지스트리
==============

**Site** = 관리자가 FMS 에 등록하는 물리 시설. Site 아래에 맵 여러 개(물리
구역별)와 로봇이 귀속된다. 로봇은 등록 시 맵 하나를 배정받는다.

    {data_root}/sites/{site_id}/
      site.json              {name, created_at, robots:{id:{map_id, registered_at}}}
      maps/{map_id}/{version}/...      (MapStore 가 관리)
      maps/{map_id}/active

영속화는 site.json 파일. DB(MySQL)는 나중. 관리자가 등록한 게 서버 재시작에
사라지면 안 되므로 모든 변경은 즉시 파일에 쓴다.

용어: 여기의 Site 는 시설. `common/map_annotations.MapAnnotations` 는 맵 하나의
location/zone 이다 (다른 개념).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fms_server.locations import LocationStore
from fms_server.mapkit.store import MapStore

log = logging.getLogger("fms.sites")

SITE_META_NAME = "site.json"
MAPS_DIR = "maps"


class SiteError(Exception):
    """일반 site 관련 오류 (요청이 잘못됨 — HTTP 422 급)."""


class SiteNotFound(SiteError):
    """site / 로봇 등록 자체를 못 찾음 (HTTP 404 급)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_id(value: str, kind: str) -> None:
    if not value or "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise SiteError(f"허용되지 않는 {kind}: {value!r}")
    if len(value) > 128:
        raise SiteError(f"{kind} 가 너무 길다 (128자 이하)")


@dataclass
class RobotAssignment:
    robot_id: str
    site_id: str
    map_id: str
    registered_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        # site_id 는 site.json 의 위치로 이미 알 수 있으므로 저장하지 않는다.
        return {"map_id": self.map_id, "registered_at": self.registered_at}


@dataclass
class Site:
    site_id: str
    name: str
    created_at: str = field(default_factory=_now_iso)
    # robot_id -> RobotAssignment
    robots: dict[str, RobotAssignment] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "created_at": self.created_at,
            "robots": {rid: a.to_dict() for rid, a in self.robots.items()},
        }

    @classmethod
    def from_dict(cls, site_id: str, data: dict) -> "Site":
        robots = {
            rid: RobotAssignment(
                robot_id=rid,
                site_id=site_id,
                map_id=entry["map_id"],
                registered_at=entry.get("registered_at", ""),
            )
            for rid, entry in (data.get("robots") or {}).items()
        }
        return cls(
            site_id=site_id,
            name=data.get("name", site_id),
            created_at=data.get("created_at", ""),
            robots=robots,
        )


class SiteRegistry:
    """
    site 목록 + 로봇 배정을 보관한다. 스레드 안전 (HTTP 핸들러 + 코디네이터가
    같이 만진다). 맵 저장소는 site 별로 `MapStore` 를 만들어 돌려준다.
    """

    def __init__(self, data_root: Path) -> None:
        self._lock = threading.RLock()
        self._sites_root = Path(data_root) / "sites"
        self._sites: dict[str, Site] = {}
        self._load_all()

    # -- 영속화 ------------------------------------------------------

    def _site_dir(self, site_id: str) -> Path:
        _check_id(site_id, "site_id")
        return self._sites_root / site_id

    def _load_all(self) -> None:
        if not self._sites_root.is_dir():
            return
        for site_dir in sorted(p for p in self._sites_root.iterdir() if p.is_dir()):
            meta = site_dir / SITE_META_NAME
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                self._sites[site_dir.name] = Site.from_dict(site_dir.name, data)
            except (json.JSONDecodeError, KeyError) as exc:
                log.error("site.json 로드 실패 %s: %s", site_dir.name, exc)
        log.info("site %d개 로드", len(self._sites))

    def _persist(self, site: Site) -> None:
        site_dir = self._site_dir(site.site_id)
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / SITE_META_NAME).write_text(
            json.dumps(site.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # -- site CRUD -------------------------------------------------

    def create_site(self, site_id: str, name: str) -> Site:
        _check_id(site_id, "site_id")
        with self._lock:
            if site_id in self._sites:
                raise SiteError(f"site {site_id} 가 이미 있다")
            site = Site(site_id=site_id, name=name or site_id)
            self._sites[site_id] = site
            self._persist(site)
            log.info("site 등록: %s (%s)", site_id, site.name)
            return site

    def list_sites(self) -> list[Site]:
        with self._lock:
            return list(self._sites.values())

    def get_site(self, site_id: str) -> Site:
        with self._lock:
            site = self._sites.get(site_id)
            if site is None:
                raise SiteNotFound(f"site {site_id} 없음")
            return site

    def delete_site(self, site_id: str, *, force: bool = False) -> None:
        import shutil

        with self._lock:
            site = self._sites.get(site_id)
            if site is None:
                raise SiteNotFound(f"site {site_id} 없음")
            if site.robots and not force:
                raise SiteError(
                    f"site {site_id} 에 로봇 {len(site.robots)}대가 등록돼 있다 "
                    "(force=true 로 강제 삭제)"
                )
            del self._sites[site_id]
            shutil.rmtree(self._site_dir(site_id), ignore_errors=True)
            log.info("site 삭제: %s", site_id)

    # -- 맵 저장소 (site 별) --------------------------------------

    def map_store(self, site_id: str) -> MapStore:
        """이 site 의 맵 저장소. `{site_dir}/maps` 에 루팅된 MapStore."""
        self.get_site(site_id)  # 존재 확인
        return MapStore(self._site_dir(site_id) / MAPS_DIR, site_id=site_id)

    def location_store(self, site_id: str) -> "LocationStore":
        """이 site 의 named location 저장소 (`{site_dir}/locations.json`)."""
        return LocationStore(self._site_dir(site_id), self.map_store(site_id))

    # -- 로봇 등록 / 배정 --------------------------------------

    def register_robot(self, site_id: str, robot_id: str, map_id: str) -> RobotAssignment:
        """로봇을 site 에 등록하고 맵을 배정한다. 이미 있으면 재배정."""
        _check_id(robot_id, "robot_id")
        _check_id(map_id, "map_id")
        with self._lock:
            site = self.get_site(site_id)
            # 배정 맵이 이 site 에 실제로 있는지 확인
            store = MapStore(self._site_dir(site_id) / MAPS_DIR, site_id=site_id)
            try:
                store.get_map(map_id)
            except Exception:
                raise SiteError(f"site {site_id} 에 맵 {map_id} 가 없다 — 먼저 업로드해라") from None

            assignment = RobotAssignment(robot_id=robot_id, site_id=site_id, map_id=map_id)
            site.robots[robot_id] = assignment
            self._persist(site)
            log.info("로봇 등록: %s -> site %s / 맵 %s", robot_id, site_id, map_id)
            return assignment

    def unregister_robot(self, site_id: str, robot_id: str) -> None:
        with self._lock:
            site = self.get_site(site_id)
            if robot_id not in site.robots:
                raise SiteNotFound(f"로봇 {robot_id} 는 site {site_id} 에 등록돼 있지 않다")
            del site.robots[robot_id]
            self._persist(site)
            log.info("로봇 등록 해제: %s (site %s)", robot_id, site_id)

    def assignment_for(self, robot_id: str) -> Optional[RobotAssignment]:
        """로봇이 어느 site 의 어느 맵에 배정됐는지. 미등록이면 None.

        (robot_id 는 전역 유일 가정 — 한 로봇이 두 site 에 등록될 수 없다.)
        """
        with self._lock:
            for site in self._sites.values():
                a = site.robots.get(robot_id)
                if a is not None:
                    return a
            return None

    def site_of_robot(self, robot_id: str) -> Optional[str]:
        with self._lock:
            for site_id, site in self._sites.items():
                if robot_id in site.robots:
                    return site_id
            return None
