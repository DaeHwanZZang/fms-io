"""맵 저장소 (store + bake) 단위 테스트. 실제 벤더 맵으로 돈다."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from common.map_model import MapModel
from fms_server.mapkit.bake import _polygon_pixel_mask, despeckle_occupied
from fms_server.mapkit.store import MapStore, MapStoreError, compute_map_uid

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")
MAP_ID = "floor-1"
SITE_ID = "hq"


def _vendor_files() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in VENDOR.iterdir() if p.is_file()}


@pytest.fixture
def store(tmp_path) -> MapStore:
    return MapStore(tmp_path / "maps", site_id=SITE_ID)


# =============================================================================
# map_uid
# =============================================================================

def test_map_uid_is_deterministic_24hex():
    a = compute_map_uid("hq", "floor-1")
    b = compute_map_uid("hq", "floor-1")
    assert a == b
    assert len(a) == 24
    assert all(c in "0123456789abcdef" for c in a)


def test_map_uid_differs_by_site_and_map():
    assert compute_map_uid("hq", "floor-1") != compute_map_uid("hq", "floor-2")
    assert compute_map_uid("hq", "floor-1") != compute_map_uid("branch", "floor-1")


def test_bundle_carries_uid_as_name(store):
    r = store.ingest_upload(MAP_ID, _vendor_files(), activate=True)
    uid = compute_map_uid(SITE_ID, MAP_ID)
    assert r.map_uid == uid

    model = store.load_map_model(MAP_ID)
    assert model.metadata.name == uid          # fms_map.json name

    ann = store.load_annotations(MAP_ID)
    assert ann.map_id == uid                    # annotations.json map_id

    manifest = store.manifest(MAP_ID)
    assert manifest["map_uid"] == uid
    assert manifest["site_id"] == SITE_ID
    assert manifest["map_id"] == MAP_ID         # 관리자 라벨은 그대로


def test_ingest_produces_bundle(store):
    r = store.ingest_upload(MAP_ID, _vendor_files(), activate=True)
    assert r.created is True
    assert len(r.version) == 12

    names = zipfile.ZipFile(io.BytesIO(store.bundle_zip(MAP_ID))).namelist()
    assert set(names) == {"fms_map.json", "navi_gridmap.baked.png", "annotations.json", "manifest.json"}


def test_ingest_is_idempotent(store):
    files = _vendor_files()
    a = store.ingest_upload(MAP_ID, files)
    b = store.ingest_upload(MAP_ID, files)
    assert a.version == b.version
    assert a.created is True and b.created is False


def test_despeckle_changes_version(store):
    files = _vendor_files()
    a = store.ingest_upload(MAP_ID, files, despeckle_iterations=0)
    b = store.ingest_upload(MAP_ID, files, despeckle_iterations=2)
    assert a.version != b.version


def test_missing_grid_cfg_rejected(store):
    files = _vendor_files()
    del files["grid_cfg.grid"]
    with pytest.raises(MapStoreError):
        store.ingest_upload(MAP_ID, files)


def test_prohibit_zones_are_baked(store):
    """구운 grid 에서 prohibit zone 영역이 FREE 가 아니어야 한다."""
    r = store.ingest_upload(MAP_ID, _vendor_files(), activate=True)
    model = store.load_map_model(MAP_ID)
    site = store.load_annotations(MAP_ID)  # activate=True 라 활성 버전으로 해석됨
    codes = model.classify_grid()

    for zone in site.prohibit_zones():
        mask = _polygon_pixel_mask(model, [(p.x, p.y) for p in zone.polygon])
        free_inside = int((mask & (codes == 2)).sum())
        assert free_inside == 0, f"zone {zone.name}: 구운 뒤에도 FREE 픽셀 {free_inside}개 남음"

    assert r.stats["zone_baked_px"] > 0


def test_annotations_theta_normalized(store):
    r = store.ingest_upload(MAP_ID, _vendor_files())
    site = store.load_annotations(MAP_ID, r.version)
    loc = site.location_by_name("SCHAE38")
    assert loc is not None
    assert -3.15 <= loc.pose.theta <= 3.15


def test_integrity_check_passes(store):
    r = store.ingest_upload(MAP_ID, _vendor_files())
    assert store.verify_integrity(MAP_ID, r.version) == []


def test_integrity_detects_tamper(store):
    r = store.ingest_upload(MAP_ID, _vendor_files())
    baked = store._version_dir(MAP_ID, r.version) / "navi_gridmap.baked.png"
    baked.write_bytes(baked.read_bytes() + b"junk")
    problems = store.verify_integrity(MAP_ID, r.version)
    assert any("navi_gridmap.baked.png" in p for p in problems)


def test_activate_and_resolve(store):
    files = _vendor_files()
    r1 = store.ingest_upload(MAP_ID, files, despeckle_iterations=0)
    r2 = store.ingest_upload(MAP_ID, files, despeckle_iterations=2)

    assert store.active_version(MAP_ID) is None
    with pytest.raises(MapStoreError):
        store.resolve_version(MAP_ID, None)

    store.activate(MAP_ID, r2.version)
    assert store.active_version(MAP_ID) == r2.version
    assert store.resolve_version(MAP_ID, None) == r2.version
    assert store.resolve_version(MAP_ID, r1.version) == r1.version


def test_activate_unknown_version_rejected(store):
    store.ingest_upload(MAP_ID, _vendor_files())
    with pytest.raises(MapStoreError):
        store.activate(MAP_ID, "deadbeefcafe")


def test_path_traversal_blocked(store):
    with pytest.raises(MapStoreError):
        store._map_dir("../etc")
    with pytest.raises(MapStoreError):
        store.file_path(MAP_ID, "../../../etc/passwd", version="x")


def test_despeckle_only_reclassifies_occupied():
    codes = np.full((5, 5), 2, dtype=np.uint8)
    codes[2, 2] = 0  # 고립 OCCUPIED 한 점
    out, removed = despeckle_occupied(codes, iterations=1)
    assert removed == 1
    assert out[2, 2] == 1  # OCCUPIED -> UNKNOWN (FREE 아님)
    assert (out == 2).sum() == 24
