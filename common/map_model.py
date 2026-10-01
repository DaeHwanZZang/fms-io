"""
맵 데이터 모델
==============

2D 흑백 occupancy grid PNG + 메타데이터(JSON)로 맵을 표현한다.
ROS map_server 의 map.pgm + map.yaml 과 같은 아이디어.

셀 분류 (그레이스케일 0~255)
---------------------------
    흰색 (>= free_thresh)      FREE      로봇이 갈 수 있는 영역
    회색 (그 사이)              UNKNOWN   가 본 적 없는 구역
    검은색 (<= occupied_thresh) OCCUPIED  장애물

**주행 가능 여부는 FREE 만 True 다.** UNKNOWN 은 OCCUPIED 와 동일하게
막힌 것으로 취급한다 (가 본 적 없는 곳으로는 보내지 않는다).
렌더링할 때는 셋을 구분해서 보여줄 수 있도록 분류는 3값으로 유지한다.

좌표계
------
world 좌표 (x, y) : 미터 단위. origin 은 이미지 **좌상단** 픽셀의 world 좌표.
pixel 좌표 (px, py): 이미지 좌상단이 (0, 0), py 는 아래로 증가 (이미지 관례).

    px = floor((world_x - origin_x) / resolution)
    py = floor((world_y - origin_y) / resolution)

즉 world y 도 pixel y 처럼 아래로 증가한다 — **y 축을 뒤집지 않는다.**
(예전엔 ROS map_server 식으로 origin=좌하단 + y 축 뒤집기였는데, 실제 AMR
현장에서 쓰는 맵(SLAM 벤더 포맷, `grid_cfg.grid` 류)이 좌상단 원점 +
뒤집지 않는 좌표계를 쓰길래 여기 맞췄다. 그래야 벤더 맵을 이미지/좌표
변환 없이 원본 그대로 쓸 수 있다.) 변환은 반드시 이 모듈의 함수를 거칠 것.

역변환(pixel_to_world)은 해당 픽셀의 **중심** world 좌표를 돌려준다.
따라서 world -> pixel -> world 는 픽셀 중심으로 스냅되며, 원래 값과
최대 resolution/2 만큼 차이날 수 있다.

경로계획(A*)은 FMS 담당이며 이 모듈에는 두지 않는다. 여기는 순수하게
"맵을 읽고 좌표를 변환하고 셀 상태를 알려주는" 역할만 한다.
"""

from __future__ import annotations

import math
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    import numpy as np


class CellType(str, Enum):
    """맵 셀 한 칸의 종류."""

    FREE = "FREE"          # 흰색. 주행 가능
    UNKNOWN = "UNKNOWN"    # 회색. 미탐사 -> 주행 불가로 취급
    OCCUPIED = "OCCUPIED"  # 검은색. 장애물

    @property
    def drivable(self) -> bool:
        return self is CellType.FREE


class MapMetadata(BaseModel):
    """맵 이미지에 딸린 메타데이터. ROS 의 map.yaml 역할."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="map", description="맵 이름")
    image: str = Field(description="맵 이미지 파일명. 메타데이터 파일과 같은 디렉터리 기준")
    resolution: float = Field(gt=0, description="픽셀당 미터 (m/px)")
    origin_x: float = Field(default=0.0, description="이미지 좌상단 픽셀의 world x (m)")
    origin_y: float = Field(default=0.0, description="이미지 좌상단 픽셀의 world y (m). y 는 아래로 증가")
    width_px: int = Field(gt=0, description="이미지 가로 픽셀 수")
    height_px: int = Field(gt=0, description="이미지 세로 픽셀 수")
    occupied_thresh: int = Field(
        default=64, ge=0, le=255, description="이 값 이하 grey = OCCUPIED (검은색)"
    )
    free_thresh: int = Field(
        default=192, ge=0, le=255, description="이 값 이상 grey = FREE (흰색)"
    )

    @model_validator(mode="after")
    def _check_thresholds(self) -> "MapMetadata":
        if self.occupied_thresh >= self.free_thresh:
            raise ValueError("occupied_thresh 는 free_thresh 보다 작아야 한다")
        return self

    @property
    def width_m(self) -> float:
        return self.width_px * self.resolution

    @property
    def height_m(self) -> float:
        return self.height_px * self.resolution


class MapModel:
    """
    맵 메타데이터 + 픽셀 데이터.

    픽셀 데이터는 처음 필요할 때 lazy 로 읽는다. 좌표 변환(world<->pixel)은
    메타데이터만으로 동작하므로 이미지 파일 없이도 쓸 수 있다.
    """

    def __init__(self, metadata: MapMetadata, base_dir: Optional[Path] = None):
        self.metadata = metadata
        self.base_dir = Path(base_dir) if base_dir is not None else Path(".")
        self._grid: Optional["np.ndarray"] = None

    @classmethod
    def load(cls, metadata_path: str | Path) -> "MapModel":
        """맵 메타데이터 JSON 을 읽어 MapModel 을 만든다."""
        metadata_path = Path(metadata_path)
        metadata = MapMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
        return cls(metadata, base_dir=metadata_path.parent)

    @property
    def image_path(self) -> Path:
        return self.base_dir / self.metadata.image

    # -- 좌표 변환 --------------------------------------------------------

    def world_to_pixel(self, x: float, y: float) -> tuple[int, int]:
        """
        world 좌표(m) -> 그 좌표를 포함하는 픽셀 인덱스.

        범위 밖이면 음수나 크기 이상의 값이 그대로 나오므로 in_bounds 로 확인할 것.
        (음수 좌표에서 0 쪽으로 절삭되면 안 되므로 int() 가 아니라 floor 를 쓴다.)
        origin 이 좌상단이고 y 축을 뒤집지 않으므로 x/y 둘 다 같은 모양의 식이다.
        """
        m = self.metadata
        px = math.floor((x - m.origin_x) / m.resolution)
        py = math.floor((y - m.origin_y) / m.resolution)
        return px, py

    def pixel_to_world(self, px: int, py: int) -> tuple[float, float]:
        """픽셀 인덱스 -> 해당 픽셀 중심의 world 좌표(m)."""
        m = self.metadata
        x = m.origin_x + (px + 0.5) * m.resolution
        y = m.origin_y + (py + 0.5) * m.resolution
        return x, y

    def in_bounds(self, x: float, y: float) -> bool:
        px, py = self.world_to_pixel(x, y)
        m = self.metadata
        return 0 <= px < m.width_px and 0 <= py < m.height_px

    @property
    def world_extent(self) -> tuple[float, float, float, float]:
        """(x_min, x_max, y_min, y_max). matplotlib imshow 의 extent 로 바로 쓸 수 있다."""
        m = self.metadata
        return (m.origin_x, m.origin_x + m.width_m, m.origin_y, m.origin_y + m.height_m)

    # -- 픽셀 데이터 ------------------------------------------------------

    @property
    def grid(self) -> "np.ndarray":
        """
        그레이스케일 픽셀 배열. shape (height_px, width_px), dtype uint8.
        인덱싱은 grid[py][px] — 이미지 관례(행 = y) 그대로다.
        """
        if self._grid is None:
            self._grid = self._load_grid()
        return self._grid

    def _load_grid(self) -> "np.ndarray":
        import numpy as np
        from PIL import Image

        img = Image.open(self.image_path).convert("L")
        m = self.metadata
        if img.size != (m.width_px, m.height_px):
            raise ValueError(
                f"이미지 크기 {img.size} 가 메타데이터 {(m.width_px, m.height_px)} 와 다르다"
            )
        return np.asarray(img, dtype=np.uint8)

    def classify_grey(self, grey: int) -> CellType:
        """그레이스케일 값 하나를 셀 종류로 분류한다."""
        m = self.metadata
        if grey >= m.free_thresh:
            return CellType.FREE
        if grey <= m.occupied_thresh:
            return CellType.OCCUPIED
        return CellType.UNKNOWN

    def cell_at(self, x: float, y: float) -> CellType:
        """world 좌표의 셀 종류. 맵 범위 밖은 OCCUPIED 로 본다."""
        if not self.in_bounds(x, y):
            return CellType.OCCUPIED
        px, py = self.world_to_pixel(x, y)
        return self.classify_grey(int(self.grid[py, px]))

    def is_free(self, x: float, y: float) -> bool:
        """world 좌표가 주행 가능한가. UNKNOWN 은 False (장애물과 동일 취급)."""
        return self.cell_at(x, y).drivable

    def classify_grid(self) -> "np.ndarray":
        """
        전체 그리드를 CellType 코드 배열로 변환한다.
        값: 0 = OCCUPIED, 1 = UNKNOWN, 2 = FREE (뷰어 컬러맵용).
        """
        import numpy as np

        m = self.metadata
        codes = np.ones(self.grid.shape, dtype=np.uint8)  # 기본 UNKNOWN
        codes[self.grid <= m.occupied_thresh] = 0
        codes[self.grid >= m.free_thresh] = 2
        return codes
