"""
벤더 맵 인제스트 — grid_cfg.grid -> fms_map.json
================================================

SLAM 벤더가 뱉는 맵 폴더 하나를 우리 계약(`common/map_model.MapMetadata`)으로
정규화한다. **`fms_map.json` 은 손으로 쓰지 않는다** — 이 모듈이 `grid_cfg.grid`
에서 생성한다.

왜 손으로 쓰면 안 되나
---------------------
`641931de9eae7cecb34d5765` 의 `fms_map.json` 은 origin 이 정확히 2배로 적혀
있었다 (`-42.253462` vs 벤더 `ox: -21.128462`). 그 상태로는 `zone_meta.json`
의 금지구역 7개 중 6개가 맵 범위 밖으로 떨어진다. 좌표계 원본은 언제나
`grid_cfg.grid` 다.

벤더 `grid_cfg.grid` 포맷 (key : value 한 줄씩)
---------------------------------------------
    ox / oy              이미지 좌상단 픽셀의 world 좌표 (m)
    origin_px/origin_py  world 원점(0,0)이 놓인 픽셀 인덱스 (검산용)
    width_gm/height_gm   이미지 픽셀 크기
    scale_m2px           1미터당 픽셀 수 -> resolution = 1 / scale_m2px

우리 좌표계와 그대로 맞는다 — 좌상단 원점, y 아래로 증가, 뒤집기 없음
(`common/map_model.py` 문서 참고).

사용법
------
    # 검증만 (기본값)
    python -m fms_server.mapkit.ingest AMR_client/maps/641931de9eae7cecb34d5765

    # fms_map.json 생성/덮어쓰기
    python -m fms_server.mapkit.ingest AMR_client/maps/641931de9eae7cecb34d5765 --write
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional

from common.map_model import CellType, MapMetadata, MapModel

# 항법용 이미지 우선순위. 앞에 있는 것부터 찾아 쓴다.
IMAGE_PREFERENCE = (
    "edited_navi_gridmap.png",
    "navi_gridmap.png",
    "grid.png",
    "border_gridmap.png",
)

GRID_CFG_NAME = "grid_cfg.grid"
MAP_META_NAME = "map_meta.json"
LOCATION_META_NAME = "location_meta.json"
ZONE_META_NAME = "zone_meta.json"
OUTPUT_NAME = "fms_map.json"


# =============================================================================
# grid_cfg.grid 파싱
# =============================================================================

class GridCfg(dict):
    """`grid_cfg.grid` 를 파싱한 원시 key -> float 맵. 없는 키 접근은 KeyError."""

    @classmethod
    def parse(cls, path: Path) -> "GridCfg":
        cfg = cls()
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"{path}:{lineno}: 'key : value' 형식이 아니다 -> {raw!r}")
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            try:
                cfg[key] = float(value)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: '{key}' 값이 숫자가 아니다 -> {value!r}") from exc
        return cfg

    def require(self, key: str, path: Path) -> float:
        if key not in self:
            raise ValueError(f"{path}: 필수 키 '{key}' 가 없다")
        return self[key]


def normalize_theta(theta: float) -> float:
    """임의 각도를 -pi ~ pi 로 접는다.

    벤더 location pose 의 theta 는 0~2pi 로 온다 (예: 4.71 = 270도). 그대로는
    `common/schemas.Pose.theta` 의 -3.15~3.15 검증에 걸린다.
    """
    return math.atan2(math.sin(theta), math.cos(theta))


# =============================================================================
# 메타데이터 생성
# =============================================================================

def pick_image(vendor_dir: Path, override: Optional[str] = None) -> str:
    """항법에 쓸 그리드 이미지 파일명을 고른다."""
    if override is not None:
        if not (vendor_dir / override).is_file():
            raise FileNotFoundError(f"{vendor_dir / override} 가 없다")
        return override
    for name in IMAGE_PREFERENCE:
        if (vendor_dir / name).is_file():
            return name
    raise FileNotFoundError(
        f"{vendor_dir}: 항법 이미지를 못 찾았다 (찾은 이름: {', '.join(IMAGE_PREFERENCE)})"
    )


def build_metadata(
    vendor_dir: Path,
    image: Optional[str] = None,
    occupied_thresh: int = 64,
    free_thresh: int = 192,
) -> tuple[MapMetadata, list[str]]:
    """벤더 폴더 하나에서 `MapMetadata` 를 만든다. (메타데이터, 경고목록) 반환."""
    vendor_dir = Path(vendor_dir)
    warnings: list[str] = []

    cfg_path = vendor_dir / GRID_CFG_NAME
    if not cfg_path.is_file():
        raise FileNotFoundError(f"{cfg_path} 가 없다 — 좌표계 원본이므로 필수")
    cfg = GridCfg.parse(cfg_path)

    scale = cfg.require("scale_m2px", cfg_path)
    if scale <= 0:
        raise ValueError(f"{cfg_path}: scale_m2px 는 양수여야 한다 (읽은 값 {scale})")

    origin_x = cfg.require("ox", cfg_path)
    origin_y = cfg.require("oy", cfg_path)
    width_px = int(cfg.require("width_gm", cfg_path))
    height_px = int(cfg.require("height_gm", cfg_path))
    resolution = 1.0 / scale

    # 검산: ox 는 world 원점 픽셀 위치와 맞아떨어져야 한다 (ox == -origin_px * resolution).
    for axis, origin_m, px_key in (("x", origin_x, "origin_px"), ("y", origin_y, "origin_py")):
        if px_key not in cfg:
            continue
        expected = -cfg[px_key] * resolution
        drift_px = abs(origin_m - expected) / resolution
        if drift_px > 1.0:
            warnings.append(
                f"origin_{axis}({origin_m:.6f}) 와 {px_key}({cfg[px_key]:.0f}) 가 "
                f"{drift_px:.2f}px 어긋난다 (기대값 {expected:.6f})"
            )

    # 맵 이름은 map_meta.json 우선, 없으면 폴더 이름.
    name = vendor_dir.name
    meta_path = vendor_dir / MAP_META_NAME
    if meta_path.is_file():
        try:
            name = json.loads(meta_path.read_text(encoding="utf-8")).get("name") or name
        except json.JSONDecodeError as exc:
            warnings.append(f"{MAP_META_NAME} 파싱 실패, 폴더 이름을 쓴다 ({exc})")

    metadata = MapMetadata(
        name=name,
        image=pick_image(vendor_dir, image),
        resolution=resolution,
        origin_x=origin_x,
        origin_y=origin_y,
        width_px=width_px,
        height_px=height_px,
        occupied_thresh=occupied_thresh,
        free_thresh=free_thresh,
    )
    return metadata, warnings


# =============================================================================
# 검증 — location / zone 이 맵 안에 들어오는지
# =============================================================================

def verify(vendor_dir: Path, metadata: MapMetadata) -> list[str]:
    """location/zone/이미지가 이 메타데이터와 앞뒤가 맞는지 본다. 문제 목록 반환."""
    vendor_dir = Path(vendor_dir)
    problems: list[str] = []
    model = MapModel(metadata, base_dir=vendor_dir)

    # -- 이미지 크기 --------------------------------------------------------
    try:
        from PIL import Image

        with Image.open(model.image_path) as img:
            if img.size != (metadata.width_px, metadata.height_px):
                problems.append(
                    f"이미지 크기 {img.size} != 메타데이터 "
                    f"({metadata.width_px}, {metadata.height_px})"
                )
    except FileNotFoundError:
        problems.append(f"이미지 {model.image_path} 가 없다")

    x_min, x_max, y_min, y_max = model.world_extent

    # -- location -----------------------------------------------------------
    loc_path = vendor_dir / LOCATION_META_NAME
    if loc_path.is_file():
        payload = json.loads(loc_path.read_text(encoding="utf-8"))
        for loc in payload.get("locations", []):
            label = loc.get("name") or loc.get("unique_id") or "<이름없음>"
            pose = loc.get("pose") or []
            if len(pose) < 2:
                problems.append(f"location '{label}': pose 가 [x, y, theta] 형태가 아니다")
                continue
            x, y = float(pose[0]), float(pose[1])
            if not model.in_bounds(x, y):
                problems.append(
                    f"location '{label}': ({x:.3f}, {y:.3f}) 가 맵 밖 "
                    f"[{x_min:.2f}, {x_max:.2f}] x [{y_min:.2f}, {y_max:.2f}]"
                )
            elif model.cell_at(x, y) is not CellType.FREE:
                problems.append(
                    f"location '{label}': ({x:.3f}, {y:.3f}) 셀이 "
                    f"{model.cell_at(x, y).value} — 주행 불가 지점에 station 이 있다"
                )
            if len(pose) >= 3:
                theta = float(pose[2])
                if not -3.15 <= theta <= 3.15:
                    problems.append(
                        f"location '{label}': theta {theta:.4f} 가 Pose 검증범위 밖 "
                        f"(정규화하면 {normalize_theta(theta):.4f})"
                    )

    # -- zone ---------------------------------------------------------------
    zone_path = vendor_dir / ZONE_META_NAME
    if zone_path.is_file():
        payload = json.loads(zone_path.read_text(encoding="utf-8"))
        for zone in payload.get("zones", []):
            label = zone.get("name") or zone.get("id") or "<이름없음>"
            polygon = zone.get("polygon") or []
            if len(polygon) < 3:
                problems.append(f"zone '{label}': 꼭짓점이 {len(polygon)}개 — 폴리곤이 아니다")
                continue
            outside = [(float(p[0]), float(p[1])) for p in polygon if not model.in_bounds(float(p[0]), float(p[1]))]
            if outside:
                sample = ", ".join(f"({x:.2f}, {y:.2f})" for x, y in outside[:3])
                problems.append(
                    f"zone '{label}' ({zone.get('type', '?')}): 꼭짓점 "
                    f"{len(outside)}/{len(polygon)} 개가 맵 밖 — {sample}"
                )

    return problems


# =============================================================================
# CLI
# =============================================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="벤더 맵 폴더에서 fms_map.json 을 생성/검증한다.",
    )
    parser.add_argument("vendor_dir", type=Path, help="맵 폴더 (grid_cfg.grid 가 있는 곳)")
    parser.add_argument("--image", default=None, help=f"항법 이미지 파일명 (기본: {IMAGE_PREFERENCE[0]} 등에서 자동)")
    parser.add_argument("--occupied-thresh", type=int, default=64)
    parser.add_argument("--free-thresh", type=int, default=192)
    parser.add_argument("--write", action="store_true", help=f"{OUTPUT_NAME} 을 실제로 쓴다 (기본은 검증만)")
    parser.add_argument("-o", "--output", type=Path, default=None, help=f"출력 경로 (기본: <vendor_dir>/{OUTPUT_NAME})")
    args = parser.parse_args(argv)

    vendor_dir: Path = args.vendor_dir
    if not vendor_dir.is_dir():
        print(f"error: {vendor_dir} 는 디렉터리가 아니다", file=sys.stderr)
        return 2

    metadata, warnings = build_metadata(
        vendor_dir,
        image=args.image,
        occupied_thresh=args.occupied_thresh,
        free_thresh=args.free_thresh,
    )
    output = args.output or (vendor_dir / OUTPUT_NAME)

    x_min, x_max, y_min, y_max = MapModel(metadata, base_dir=vendor_dir).world_extent
    print(f"[map]    {metadata.name}  image={metadata.image}")
    print(f"[grid]   {metadata.width_px} x {metadata.height_px} px @ {metadata.resolution} m/px")
    print(f"[origin] ({metadata.origin_x:.6f}, {metadata.origin_y:.6f})")
    print(f"[extent] x [{x_min:.3f}, {x_max:.3f}]  y [{y_min:.3f}, {y_max:.3f}]")

    # 기존 파일과 달라졌으면 어디가 달라졌는지 보여준다.
    if output.is_file():
        try:
            old = MapMetadata.model_validate_json(output.read_text(encoding="utf-8"))
        except Exception as exc:  # 형식이 깨진 기존 파일도 그냥 덮어쓸 수 있어야 한다
            print(f"[diff]   기존 {output.name} 을 못 읽었다 ({exc})")
        else:
            diffs = [
                f"{field}: {getattr(old, field)!r} -> {getattr(metadata, field)!r}"
                for field in MapMetadata.model_fields
                if getattr(old, field) != getattr(metadata, field)
            ]
            for line in diffs:
                print(f"[diff]   {line}")
            if not diffs:
                print(f"[diff]   기존 {output.name} 과 동일")

    for warning in warnings:
        print(f"[warn]   {warning}")

    problems = verify(vendor_dir, metadata)
    for problem in problems:
        print(f"[FAIL]   {problem}")
    if not problems:
        print("[ok]     location / zone 전부 맵 범위 안, 셀 검사 통과")

    if args.write:
        output.write_text(metadata.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"[write]  {output}")
    else:
        print("[dry]    --write 를 붙여야 실제로 쓴다")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
