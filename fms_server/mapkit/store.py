"""
맵 저장소
=========

벤더 맵 폴더를 업로드받아 인제스트(정규화 + 굽기)한 뒤 버전을 매겨 보관한다.
로봇은 여기서 **구워진 번들**만 HTTP 로 받아 간다.

디스크 레이아웃
--------------
    data/maps/{map_id}/
      {version}/
        fms_map.json              image -> navi_gridmap.baked.png 를 가리킨다
        navi_gridmap.baked.png    despeckle + prohibit zone 구운 grid (로봇이 쓰는 것)
        annotations.json                 정규화된 MapAnnotations (location/zone)
        manifest.json             {map_id, version, files:{name:sha256}, ...}
        source/                   벤더 원본 파일 그대로 (재굽기용, 번들엔 미포함)
      active                      활성 버전 문자열 한 줄

버전
----
`fms_map.json` + `navi_gridmap.baked.png` + `annotations.json` 의 sha256 을 정렬·결합해
다시 해시한 것의 앞 12자. 같은 입력 + 같은 despeckle 설정이면 같은 버전이 나온다
(멱등). manifest.json 은 자기 자신을 해시에 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common.map_model import MapMetadata, MapModel
from fms_server.mapkit import ingest
from fms_server.mapkit.bake import bake, save_baked_png

log = logging.getLogger("fms.mapkit.store")

BAKED_IMAGE_NAME = "navi_gridmap.baked.png"
FMS_MAP_NAME = "fms_map.json"
ANNOTATIONS_NAME = "annotations.json"
MANIFEST_NAME = "manifest.json"
ACTIVE_NAME = "active"
SOURCE_DIR = "source"

# 로봇 번들에 들어가는 파일 (source/ 는 제외)
BUNDLE_FILES = (FMS_MAP_NAME, BAKED_IMAGE_NAME, ANNOTATIONS_NAME, MANIFEST_NAME)

# 버전 해시 계산에 쓰는 파일 (manifest 제외 — 닭과 달걀)
_HASHED_FILES = (FMS_MAP_NAME, BAKED_IMAGE_NAME, ANNOTATIONS_NAME)


class MapStoreError(Exception):
    pass


@dataclass
class IngestResult:
    map_id: str             # 관리자 라벨
    map_uid: str            # FMS 전역 고유 id (로봇이 보는 이름)
    version: str
    created: bool           # False = 같은 버전이 이미 있었음 (멱등)
    warnings: list[str]
    stats: dict


@dataclass
class VersionInfo:
    version: str
    created_at: str
    is_active: bool


@dataclass
class MapSummary:
    map_id: str
    map_uid: str
    active_version: Optional[str]
    versions: list[VersionInfo]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_map_uid(site_id: str, map_id: str) -> str:
    """
    FMS 전역 고유 맵 id. site_id + map_id 의 sha256 앞 24자 (벤더 ObjectId 와
    같은 모양). 결정적 — 같은 site+구역명이면 항상 같은 uid.

    로봇은 이 값을 맵의 "이름" 으로 본다 (`fms_map.json` 의 name,
    `annotations.json` 의 map_id, SET_MAP params, State.map_id). 관리자가 쓰는
    `map_id`("floor-1")는 site 안에서만 유일하고 대시보드 표시·저장 폴더명 용도.

    버전은 uid 에 안 들어간다: uid = 맵 정체성(물리 구역), version = 개정.
    """
    return hashlib.sha256(f"{site_id}/{map_id}".encode("utf-8")).hexdigest()[:24]


class MapStore:
    def __init__(self, root: Path, site_id: str = "") -> None:
        # 첫 쓰기 전까지 디렉터리를 만들지 않는다 (서버 기동/테스트에서 빈 폴더가
        # 생기는 걸 피한다). 읽기 경로는 없는 폴더를 그냥 "맵 없음" 으로 다룬다.
        self.root = Path(root)
        # map_uid 계산에 쓴다. site 밖에서 (테스트 등) MapStore 를 직접 만들면
        # 빈 문자열 — uid 는 map_id 만으로 계산된다.
        self.site_id = site_id

    def map_uid(self, map_id: str) -> str:
        return compute_map_uid(self.site_id, map_id)

    # -- 경로 헬퍼 ------------------------------------------------------

    def _map_dir(self, map_id: str) -> Path:
        _check_id(map_id)
        return self.root / map_id

    def _version_dir(self, map_id: str, version: str) -> Path:
        _check_id(version)
        return self._map_dir(map_id) / version

    # -- 인제스트 ------------------------------------------------------

    def ingest_upload(
        self,
        map_id: str,
        files: dict[str, bytes],
        *,
        despeckle_iterations: int = 0,
        activate: bool = False,
    ) -> IngestResult:
        """
        벤더 파일 묶음을 받아 인제스트한다. `files` 는 {파일명: 내용} — 벤더 폴더를
        그대로 올린 것. `grid_cfg.grid` 는 필수.
        """
        _check_id(map_id)
        if "grid_cfg.grid" not in files:
            raise MapStoreError("grid_cfg.grid 가 없다 — 좌표계 원본이므로 필수")

        with tempfile.TemporaryDirectory(prefix="fms-map-ingest-") as tmp:
            vendor_dir = Path(tmp) / "vendor"
            vendor_dir.mkdir()
            for name, data in files.items():
                safe = _safe_filename(name)
                (vendor_dir / safe).write_bytes(data)

            metadata, meta_warnings = ingest.build_metadata(vendor_dir)
            problems = ingest.verify(vendor_dir, metadata)
            # verify 문제는 치명적이지 않다 (경고로 올린다). 좌표계가 명백히 깨진
            # 경우는 build_metadata 가 이미 예외를 던졌다.

            uid = self.map_uid(map_id)  # 로봇이 보는 맵 이름 (전역 고유)

            # bake 에 map_id 자리로 uid 를 넘긴다 → annotations.json 의 map_id 가 uid 가 된다.
            # 임시로 version="pending", 해시 확정 후 다시 쓴다.
            result = bake(
                vendor_dir, metadata, uid, "pending",
                despeckle_iterations=despeckle_iterations,
            )

            staging = Path(tmp) / "staging"
            staging.mkdir()
            (staging / SOURCE_DIR).mkdir()
            for name, data in files.items():
                (staging / SOURCE_DIR / _safe_filename(name)).write_bytes(data)

            save_baked_png(result.baked_grey, staging / BAKED_IMAGE_NAME)

            # fms_map.json 의 name = uid. 로봇이 MapModel.metadata.name 으로 맵을 판별한다.
            baked_meta = metadata.model_copy(update={"image": BAKED_IMAGE_NAME, "name": uid})
            (staging / FMS_MAP_NAME).write_text(
                baked_meta.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )

            # annotations.json — 버전 확정 전이라 일단 "pending" 으로 직렬화하고,
            # 해시는 map_version 필드를 뺀 상태로 계산해서 순환을 피한다.
            ann_wo_version = result.annotations.model_dump()
            ann_wo_version["map_version"] = ""
            ann_hash_input = json.dumps(ann_wo_version, sort_keys=True).encode("utf-8")

            file_hashes = {
                FMS_MAP_NAME: _sha256_file(staging / FMS_MAP_NAME),
                BAKED_IMAGE_NAME: _sha256_file(staging / BAKED_IMAGE_NAME),
                ANNOTATIONS_NAME: _sha256_bytes(ann_hash_input),
            }
            version = _compute_version(file_hashes)

            # 이제 진짜 버전으로 annotations.json 확정
            final_ann = result.annotations.model_copy(update={"map_version": version})
            (staging / ANNOTATIONS_NAME).write_text(
                final_ann.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )

            all_warnings = list(meta_warnings) + [f"verify: {p}" for p in problems] + result.warnings

            manifest = {
                "map_uid": uid,
                "map_id": map_id,
                "site_id": self.site_id,
                "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_name": metadata.name,
                "despeckle_iterations": despeckle_iterations,
                "resolution": metadata.resolution,
                "origin": [metadata.origin_x, metadata.origin_y],
                "size_px": [metadata.width_px, metadata.height_px],
                "files": file_hashes,
                "stats": result.stats,
                "warnings": all_warnings,
            }
            (staging / MANIFEST_NAME).write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            target = self._version_dir(map_id, version)
            created = not target.exists()
            if created:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staging), str(target))
                log.info("맵 %s 버전 %s 저장 (%d 경고)", map_id, version, len(all_warnings))
            else:
                log.info("맵 %s 버전 %s 이미 존재 — 스킵 (멱등)", map_id, version)

            if activate:
                self.activate(map_id, version)

            return IngestResult(
                map_id=map_id,
                map_uid=uid,
                version=version,
                created=created,
                warnings=all_warnings,
                stats=result.stats,
            )

    # -- 조회 --------------------------------------------------------

    def list_maps(self) -> list[MapSummary]:
        if not self.root.is_dir():
            return []
        out: list[MapSummary] = []
        for map_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            out.append(self.get_map(map_dir.name))
        return out

    def get_map(self, map_id: str) -> MapSummary:
        map_dir = self._map_dir(map_id)
        if not map_dir.is_dir():
            raise MapStoreError(f"맵 {map_id} 없음")
        active = self.active_version(map_id)
        versions: list[VersionInfo] = []
        for vdir in sorted(p for p in map_dir.iterdir() if p.is_dir()):
            manifest = _read_json(vdir / MANIFEST_NAME) or {}
            versions.append(
                VersionInfo(
                    version=vdir.name,
                    created_at=manifest.get("created_at", ""),
                    is_active=(vdir.name == active),
                )
            )
        versions.sort(key=lambda v: v.created_at)
        return MapSummary(
            map_id=map_id, map_uid=self.map_uid(map_id), active_version=active, versions=versions
        )

    def resolve_version(self, map_id: str, version: Optional[str]) -> str:
        if version:
            if not self._version_dir(map_id, version).is_dir():
                raise MapStoreError(f"맵 {map_id} 에 버전 {version} 없음")
            return version
        active = self.active_version(map_id)
        if not active:
            raise MapStoreError(f"맵 {map_id} 에 활성 버전이 없다 (activate 먼저)")
        return active

    def manifest(self, map_id: str, version: Optional[str] = None) -> dict:
        v = self.resolve_version(map_id, version)
        data = _read_json(self._version_dir(map_id, v) / MANIFEST_NAME)
        if data is None:
            raise MapStoreError(f"{map_id}/{v}: manifest 없음")
        return data

    def file_path(self, map_id: str, filename: str, version: Optional[str] = None) -> Path:
        v = self.resolve_version(map_id, version)
        safe = _safe_filename(filename)
        if safe not in BUNDLE_FILES:
            raise MapStoreError(f"{filename} 는 다운로드 대상이 아니다 (허용: {', '.join(BUNDLE_FILES)})")
        path = self._version_dir(map_id, v) / safe
        if not path.is_file():
            raise MapStoreError(f"{map_id}/{v}/{safe} 없음")
        return path

    def bundle_zip(self, map_id: str, version: Optional[str] = None) -> bytes:
        v = self.resolve_version(map_id, version)
        vdir = self._version_dir(map_id, v)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in BUNDLE_FILES:
                path = vdir / name
                if path.is_file():
                    zf.write(path, arcname=name)
        return buf.getvalue()

    def verify_integrity(self, map_id: str, version: Optional[str] = None) -> list[str]:
        """manifest 의 sha256 과 실제 파일이 일치하는지. 불일치 목록 반환 (비면 정상)."""
        v = self.resolve_version(map_id, version)
        vdir = self._version_dir(map_id, v)
        manifest = self.manifest(map_id, v)
        problems: list[str] = []
        for name, expected in manifest.get("files", {}).items():
            path = vdir / name
            if not path.is_file():
                problems.append(f"{name}: 파일 없음")
                continue
            if name == ANNOTATIONS_NAME:
                continue  # annotations.json 해시는 map_version 필드를 뺀 값 기준이라 파일과 다름
            actual = _sha256_file(path)
            if actual != expected:
                problems.append(f"{name}: 해시 불일치")
        return problems

    # -- 활성 버전 ---------------------------------------------------

    def active_version(self, map_id: str) -> Optional[str]:
        marker = self._map_dir(map_id) / ACTIVE_NAME
        if not marker.is_file():
            return None
        return marker.read_text(encoding="utf-8").strip() or None

    def activate(self, map_id: str, version: str) -> None:
        if not self._version_dir(map_id, version).is_dir():
            raise MapStoreError(f"맵 {map_id} 에 버전 {version} 없음 — activate 불가")
        (self._map_dir(map_id) / ACTIVE_NAME).write_text(version + "\n", encoding="utf-8")
        log.info("맵 %s 활성 버전 -> %s", map_id, version)

    # -- 로드 (FMS 내부에서 site/맵 쓸 때) --------------------------

    def load_annotations(self, map_id: str, version: Optional[str] = None):
        from common.map_annotations import MapAnnotations

        v = self.resolve_version(map_id, version)
        raw = (self._version_dir(map_id, v) / ANNOTATIONS_NAME).read_text(encoding="utf-8")
        return MapAnnotations.model_validate_json(raw)

    def load_map_model(self, map_id: str, version: Optional[str] = None) -> MapModel:
        v = self.resolve_version(map_id, version)
        return MapModel.load(self._version_dir(map_id, v) / FMS_MAP_NAME)


# =============================================================================
# 헬퍼
# =============================================================================

def _compute_version(file_hashes: dict[str, str]) -> str:
    joined = "\n".join(f"{name}:{file_hashes[name]}" for name in sorted(_HASHED_FILES))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _check_id(value: str) -> None:
    if not value or "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise MapStoreError(f"허용되지 않는 식별자: {value!r}")


def _safe_filename(name: str) -> str:
    base = Path(name).name
    if not base or base.startswith("."):
        raise MapStoreError(f"허용되지 않는 파일명: {name!r}")
    return base


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
