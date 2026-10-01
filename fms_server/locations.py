"""
이름 붙은 좌표 (named location)
===============================

관리자가 `(x, y, theta)` 에 이름을 붙여 저장한다. job 은 나중에 raw 좌표 대신
이 이름으로 목적지를 지정하고, allocator 가 여기서 `Pose` 로 풀어 Order 에 싣는다.

**site 스코프.** 이름은 site 전체에서 유일하다. 좌표는 맵 프레임이므로 location
마다 `map_id` 를 함께 들고 있다 (한 site 에 맵이 여럿이면 프레임이 다르다).

    {data_root}/sites/{site_id}/locations.json
      { "locations": { "<name>": {map_id, x, y, theta, type, created_at, updated_at} } }

벤더 맵의 `annotations.json` location 과는 별개다. 그쪽은 버전별 콘텐츠 해시라
불변 — 여기는 관리자가 자유롭게 추가/수정하는 mutable 파일.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common.map_annotations import LocationType, normalize_theta
from common.schemas import Pose
from fms_server.mapkit.store import MapStore, MapStoreError

log = logging.getLogger("fms.locations")

LOCATIONS_FILE = "locations.json"


class LocationError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class LocationNotFound(LocationError):
    """해당 이름의 location 이 없음 (HTTP 404 급)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise LocationError(f"허용되지 않는 location 이름: {name!r}")
    if len(name) > 128:
        raise LocationError("location 이름이 너무 길다 (128자 이하)")


@dataclass
class NamedLocation:
    name: str
    map_id: str          # 이 좌표가 속한 맵 프레임 (관리자 라벨)
    x: float
    y: float
    theta: float = 0.0
    type: str = LocationType.WAYPOINT.value
    # [homework_iot 추가] True 면 job 중간 경유지에서도 멈춰 theta 로 정렬한다 (task.compile_to_order 참고)
    keep_theta: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def pose(self) -> Pose:
        return Pose(x=self.x, y=self.y, theta=self.theta)

    def to_dict(self) -> dict:
        # name 은 파일에서 key 로 쓰므로 값에는 넣지 않는다.
        return {
            "map_id": self.map_id,
            "x": self.x,
            "y": self.y,
            "theta": self.theta,
            "type": self.type,
            "keep_theta": self.keep_theta,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "NamedLocation":
        return cls(
            name=name,
            map_id=data["map_id"],
            x=float(data["x"]),
            y=float(data["y"]),
            theta=float(data.get("theta", 0.0)),
            type=data.get("type", LocationType.WAYPOINT.value),
            keep_theta=bool(data.get("keep_theta", False)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


class LocationStore:
    """
    site 하나의 named location 을 `locations.json` 에 보관한다. 스레드 안전.
    파일은 작아서 매 연산마다 통째로 읽고 쓴다 (site.json 과 동일한 방식).
    """

    def __init__(self, site_dir: Path, map_store: MapStore) -> None:
        self._path = Path(site_dir) / LOCATIONS_FILE
        self._maps = map_store
        self._lock = threading.Lock()

    # -- 영속화 --------------------------------------------------------

    def _load(self) -> dict[str, NamedLocation]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error("locations.json 로드 실패 %s: %s", self._path, exc)
            return {}
        out: dict[str, NamedLocation] = {}
        for name, entry in (raw.get("locations") or {}).items():
            try:
                out[name] = NamedLocation.from_dict(name, entry)
            except (KeyError, ValueError) as exc:
                log.error("location %r 엔트리 무시: %s", name, exc)
        return out

    def _save(self, locs: dict[str, NamedLocation]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"locations": {n: loc.to_dict() for n, loc in sorted(locs.items())}}
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # -- 검증 --------------------------------------------------------

    def _require_map(self, map_id: str) -> None:
        try:
            self._maps.get_map(map_id)
        except MapStoreError:
            raise LocationError(f"맵 {map_id!r} 가 이 site 에 없다 — 먼저 업로드해라") from None

    def _pose_warnings(self, map_id: str, x: float, y: float) -> list[str]:
        """좌표가 맵 안이고 주행 가능 셀인지. 치명적이지 않으니 경고로만."""
        try:
            model = self._maps.load_map_model(map_id)
        except MapStoreError:
            return ["맵에 활성 버전이 없어 좌표 검증을 생략했다"]
        warnings: list[str] = []
        if not model.in_bounds(x, y):
            x0, x1, y0, y1 = model.world_extent
            warnings.append(
                f"({x:.3f}, {y:.3f}) 가 맵 범위 밖 [{x0:.2f}, {x1:.2f}] x [{y0:.2f}, {y1:.2f}]"
            )
        elif not model.is_free(x, y):
            warnings.append(f"({x:.3f}, {y:.3f}) 셀이 {model.cell_at(x, y).value} — 주행 불가 지점")
        return warnings

    @staticmethod
    def _coerce_type(raw: str) -> str:
        return LocationType(raw).value  # 모르는 값은 _missing_ 으로 OTHER

    # -- 조회 --------------------------------------------------------

    def list(self) -> list[NamedLocation]:
        with self._lock:
            return sorted(self._load().values(), key=lambda loc: loc.name)

    def get(self, name: str) -> NamedLocation:
        with self._lock:
            loc = self._load().get(name)
        if loc is None:
            raise LocationNotFound(f"location {name!r} 없음")
        return loc

    def resolve_pose(self, name: str) -> tuple[str, Pose]:
        """이름 -> (map_id, Pose). allocator 가 목적지 지정에 쓴다."""
        loc = self.get(name)
        return loc.map_id, loc.pose

    # -- 쓰기 --------------------------------------------------------

    def create(
        self,
        name: str,
        map_id: str,
        x: float,
        y: float,
        theta: float = 0.0,
        type: str = LocationType.WAYPOINT.value,
        keep_theta: bool = False,
    ) -> tuple[NamedLocation, list[str]]:
        _check_name(name)
        theta = normalize_theta(theta)
        loc_type = self._coerce_type(type)
        with self._lock:
            locs = self._load()
            if name in locs:
                raise LocationError(f"location {name!r} 가 이미 있다")
            self._require_map(map_id)
            pose = Pose(x=x, y=y, theta=theta)  # theta 범위 검증
            now = _now_iso()
            loc = NamedLocation(
                name=name, map_id=map_id, x=pose.x, y=pose.y, theta=pose.theta,
                type=loc_type, keep_theta=keep_theta, created_at=now, updated_at=now,
            )
            locs[name] = loc
            self._save(locs)
        log.info("location 등록: %s -> 맵 %s (%.3f, %.3f, %.3f)", name, map_id, loc.x, loc.y, loc.theta)
        return loc, self._pose_warnings(loc.map_id, loc.x, loc.y)

    def update(
        self,
        name: str,
        *,
        map_id: Optional[str] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        theta: Optional[float] = None,
        type: Optional[str] = None,
        keep_theta: Optional[bool] = None,
    ) -> tuple[NamedLocation, list[str]]:
        with self._lock:
            locs = self._load()
            loc = locs.get(name)
            if loc is None:
                raise LocationNotFound(f"location {name!r} 없음")
            new_map = map_id if map_id is not None else loc.map_id
            new_x = x if x is not None else loc.x
            new_y = y if y is not None else loc.y
            new_theta = normalize_theta(theta) if theta is not None else loc.theta
            new_type = self._coerce_type(type) if type is not None else loc.type
            if map_id is not None:
                self._require_map(new_map)
            pose = Pose(x=new_x, y=new_y, theta=new_theta)
            loc = NamedLocation(
                name=name, map_id=new_map, x=pose.x, y=pose.y, theta=pose.theta,
                type=new_type,
                keep_theta=keep_theta if keep_theta is not None else loc.keep_theta,
                created_at=loc.created_at, updated_at=_now_iso(),
            )
            locs[name] = loc
            self._save(locs)
        log.info("location 수정: %s -> 맵 %s (%.3f, %.3f, %.3f)", name, loc.map_id, loc.x, loc.y, loc.theta)
        return loc, self._pose_warnings(loc.map_id, loc.x, loc.y)

    def delete(self, name: str) -> None:
        with self._lock:
            locs = self._load()
            if name not in locs:
                raise LocationNotFound(f"location {name!r} 없음")
            del locs[name]
            self._save(locs)
        log.info("location 삭제: %s", name)
