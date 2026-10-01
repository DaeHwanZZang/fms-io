"""
맵 굽기 — 로봇이 실제로 주행에 쓸 grid 를 만든다
==============================================

벤더 항법 이미지(그레이스케일 occupancy)에 두 가지를 적용해 `*.baked.png` 를
만든다:

1. **despeckle** — SLAM 이 남긴 고립 노이즈 픽셀 제거 (형태학적 opening).
   기본은 보수적(3x3 salt 만). 벤더 맵 품질에 따라 세기 조절.
2. **prohibit zone 굽기** — `zone_meta.json` 의 진입금지 다각형을 OCCUPIED 로
   칠한다. 이러면 로봇의 A-star (`AMR_client/planner.cpp`)는 코드 한 줄 안 고쳐도
   금지구역을 피한다 — planner 는 "grid 값이 2(FREE) 가 아니면 막힘" 으로 보니까.

또 정규화된 `MapAnnotations` (annotations.json) 을 만든다: location pose 의 theta 를
-pi~pi 로 접고, 벤더 자유문자열 type 을 우리 enum 으로 매핑한다.

출력 grid 값 규칙 (common/map_model.classify_grid 와 동일)
    0 = OCCUPIED, 128 = UNKNOWN, 255 = FREE
로봇/뷰어 모두 이 3값 그레이스케일을 읽는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from common.map_model import MapMetadata, MapModel
from common.schemas import Point, Pose
from common.map_annotations import (
    Location,
    LocationType,
    MapAnnotations,
    Zone,
    ZoneType,
    normalize_theta,
)

log = logging.getLogger("fms.mapkit.bake")

# classify_grid 코드 -> 출력 그레이 값
_CODE_TO_GREY = np.array([0, 128, 255], dtype=np.uint8)


@dataclass
class BakeResult:
    baked_grey: np.ndarray            # (h, w) uint8, 0/128/255
    annotations: MapAnnotations
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# =============================================================================
# 형태학 (numpy only — scipy 없음)
# =============================================================================

def _erode(mask: np.ndarray) -> np.ndarray:
    """4-이웃 침식. mask: bool, True = 대상 픽셀."""
    out = mask.copy()
    out[1:, :] &= mask[:-1, :]
    out[:-1, :] &= mask[1:, :]
    out[:, 1:] &= mask[:, :-1]
    out[:, :-1] &= mask[:, 1:]
    return out


def _dilate(mask: np.ndarray) -> np.ndarray:
    """4-이웃 팽창."""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def despeckle_occupied(codes: np.ndarray, iterations: int = 0) -> tuple[np.ndarray, int]:
    """
    OCCUPIED(코드 0) 에서 고립 노이즈를 제거한다. opening = erode^k then dilate^k.
    제거된 픽셀은 UNKNOWN(코드 1)으로 바꾼다.

    주의: 현재 구현은 OCCUPIED -> UNKNOWN 재분류일 뿐이라 **주행성에는 영향이
    없다** (로봇 planner 는 FREE(2) 만 통과 — OCCUPIED 든 UNKNOWN 이든 막힘).
    뷰어 표시가 검정->회색으로 바뀌는 정도. 자유공간 한가운데의 고립 OCCUPIED
    노이즈로 길이 막히는 걸 뚫으려면 FREE 로 되돌려야 하는데, 그건 "안 가 본
    곳으로 안 보낸다" 원칙과 충돌해서 별도 설계가 필요하다 (CLAUDE.md 후순위).
    그래서 기본값 0 (off).

    iterations=0 이면 아무것도 안 한다. 반환: (새 codes, 제거된 픽셀 수)
    """
    if iterations <= 0:
        return codes, 0

    occ = codes == 0
    opened = occ
    for _ in range(iterations):
        opened = _erode(opened)
    for _ in range(iterations):
        opened = _dilate(opened)
    opened &= occ  # 팽창이 원래 없던 곳으로 새면 안 된다

    removed = occ & ~opened
    count = int(removed.sum())
    if count:
        new_codes = codes.copy()
        new_codes[removed] = 1
        return new_codes, count
    return codes, 0


# =============================================================================
# zone 다각형 -> 픽셀 마스크
# =============================================================================

def _polygon_pixel_mask(model: MapModel, polygon_world: list[tuple[float, float]]) -> np.ndarray:
    """world 다각형을 (h, w) bool 마스크로 래스터화한다."""
    from PIL import Image, ImageDraw

    m = model.metadata
    pixel_pts = []
    for wx, wy in polygon_world:
        px, py = model.world_to_pixel(wx, wy)
        pixel_pts.append((px, py))

    img = Image.new("1", (m.width_px, m.height_px), 0)
    if len(pixel_pts) >= 3:
        ImageDraw.Draw(img).polygon(pixel_pts, fill=1)
    return np.asarray(img, dtype=bool)


# =============================================================================
# MapAnnotations 빌드 (벤더 location/zone -> 정규화)
# =============================================================================

def _vendor_location_type(raw: str) -> LocationType:
    try:
        return LocationType(raw)
    except ValueError:
        return LocationType.OTHER


def build_annotations(
    vendor_dir: Path,
    map_id: str,
    map_version: str,
    model: MapModel,
) -> tuple[MapAnnotations, list[str]]:
    warnings: list[str] = []
    locations: list[Location] = []
    zones: list[Zone] = []

    loc_path = vendor_dir / "location_meta.json"
    if loc_path.is_file():
        payload = json.loads(loc_path.read_text(encoding="utf-8"))
        for entry in payload.get("locations", []):
            pose_raw = entry.get("pose") or []
            if len(pose_raw) < 2:
                warnings.append(f"location {entry.get('name', '?')}: pose 형식 이상, 건너뜀")
                continue
            theta = normalize_theta(float(pose_raw[2])) if len(pose_raw) >= 3 else 0.0
            loc_id = entry.get("unique_id") or entry.get("name")
            if not loc_id:
                warnings.append("location: unique_id/name 둘 다 없음, 건너뜀")
                continue
            locations.append(
                Location(
                    location_id=str(loc_id),
                    name=str(entry.get("name") or loc_id),
                    type=_vendor_location_type(str(entry.get("type", "other"))),
                    pose=Pose(x=float(pose_raw[0]), y=float(pose_raw[1]), theta=theta),
                    marker_id=entry.get("marker_id"),
                )
            )
            if not model.in_bounds(float(pose_raw[0]), float(pose_raw[1])):
                warnings.append(f"location {entry.get('name')}: 맵 밖 좌표")

    zone_path = vendor_dir / "zone_meta.json"
    if zone_path.is_file():
        payload = json.loads(zone_path.read_text(encoding="utf-8"))
        for entry in payload.get("zones", []):
            poly = entry.get("polygon") or []
            if len(poly) < 3:
                warnings.append(f"zone {entry.get('name', '?')}: 꼭짓점 부족, 건너뜀")
                continue
            try:
                ztype = ZoneType(str(entry.get("type", "prohibit")))
            except ValueError:
                ztype = ZoneType.PROHIBIT
                warnings.append(f"zone {entry.get('name')}: 미지 type '{entry.get('type')}' -> prohibit")
            zones.append(
                Zone(
                    zone_id=str(entry.get("id") or entry.get("name")),
                    name=str(entry.get("name", "")),
                    type=ztype,
                    polygon=[Point(x=float(p[0]), y=float(p[1])) for p in poly],
                )
            )

    annotations = MapAnnotations(map_id=map_id, map_version=map_version, locations=locations, zones=zones)
    return annotations, warnings


# =============================================================================
# 굽기 본체
# =============================================================================

def bake(
    vendor_dir: Path,
    metadata: MapMetadata,
    map_id: str,
    map_version: str,
    *,
    despeckle_iterations: int = 0,
) -> BakeResult:
    """
    벤더 폴더 + 이미 만들어진 MapMetadata 로 baked grid + MapAnnotations 을 만든다.
    (metadata 는 `ingest.build_metadata` 결과를 넘긴다.)
    """
    vendor_dir = Path(vendor_dir)
    model = MapModel(metadata, base_dir=vendor_dir)
    warnings: list[str] = []

    codes = model.classify_grid()  # (h, w) 0/1/2
    total = codes.size
    occ_before = int((codes == 0).sum())

    codes, despeckled = despeckle_occupied(codes, despeckle_iterations)
    if despeckled:
        warnings.append(f"despeckle: 고립 OCCUPIED 픽셀 {despeckled}개 제거")

    annotations, ann_warnings = build_annotations(vendor_dir, map_id, map_version, model)
    warnings.extend(ann_warnings)

    baked_from_zones = 0
    for zone in annotations.prohibit_zones():
        mask = _polygon_pixel_mask(model, [(p.x, p.y) for p in zone.polygon])
        newly = int((mask & (codes == 2)).sum())  # FREE 였다가 막히는 픽셀만 카운트
        codes[mask] = 0
        baked_from_zones += newly
    if annotations.prohibit_zones():
        warnings.append(
            f"prohibit zone {len(annotations.prohibit_zones())}개 구움 "
            f"(FREE→OCCUPIED 픽셀 {baked_from_zones}개)"
        )

    baked_grey = _CODE_TO_GREY[codes]
    occ_after = int((codes == 0).sum())

    return BakeResult(
        baked_grey=baked_grey,
        annotations=annotations,
        warnings=warnings,
        stats={
            "total_px": total,
            "occupied_before": occ_before,
            "occupied_after": occ_after,
            "despeckled_px": despeckled,
            "zone_baked_px": baked_from_zones,
            "free_px": int((codes == 2).sum()),
        },
    )


def save_baked_png(baked_grey: np.ndarray, path: Path) -> None:
    from PIL import Image

    Image.fromarray(baked_grey, mode="L").save(path)
