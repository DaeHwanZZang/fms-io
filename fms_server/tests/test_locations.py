"""LocationStore 단위 테스트 (영속화 포함)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from fms_server.locations import LocationError, LocationNotFound
from fms_server.sites import SiteRegistry

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")


def _vendor_files() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in VENDOR.iterdir() if p.is_file()}


@pytest.fixture
def registry(tmp_path) -> SiteRegistry:
    r = SiteRegistry(tmp_path)
    r.create_site("hq", "본사")
    r.map_store("hq").ingest_upload("floor-1", _vendor_files(), activate=True)
    return r


def test_create_and_get(registry):
    store = registry.location_store("hq")
    loc, _warns = store.create("충전소-A", "floor-1", 1.0, 2.0, 0.0, "charger")
    assert loc.name == "충전소-A"
    assert loc.map_id == "floor-1"
    assert loc.type == "charger"
    assert store.get("충전소-A").x == 1.0
    assert [x.name for x in store.list()] == ["충전소-A"]


def test_name_collision_rejected(registry):
    store = registry.location_store("hq")
    store.create("dock", "floor-1", 1.0, 2.0)
    with pytest.raises(LocationError):
        store.create("dock", "floor-1", 3.0, 4.0)


def test_unknown_map_rejected(registry):
    store = registry.location_store("hq")
    with pytest.raises(LocationError):
        store.create("x", "ghost-map", 1.0, 2.0)


def test_theta_normalized(registry):
    store = registry.location_store("hq")
    loc, _ = store.create("t", "floor-1", 1.0, 2.0, theta=math.radians(270))
    assert -math.pi <= loc.theta <= math.pi
    assert loc.theta == pytest.approx(math.radians(270) - 2 * math.pi)


def test_unknown_type_falls_back_to_other(registry):
    store = registry.location_store("hq")
    loc, _ = store.create("weird", "floor-1", 1.0, 2.0, type="벤더자유문자열")
    assert loc.type == "other"


def test_update_partial(registry):
    store = registry.location_store("hq")
    store.create("p", "floor-1", 1.0, 2.0, 0.0, "station")
    loc, _ = store.update("p", x=5.0)
    assert loc.x == 5.0 and loc.y == 2.0 and loc.type == "station"
    assert store.get("p").created_at == loc.created_at
    assert loc.updated_at >= loc.created_at


def test_update_missing_404(registry):
    with pytest.raises(LocationNotFound):
        registry.location_store("hq").update("nope", x=1.0)


def test_delete(registry):
    store = registry.location_store("hq")
    store.create("d", "floor-1", 1.0, 2.0)
    store.delete("d")
    assert store.list() == []
    with pytest.raises(LocationNotFound):
        store.delete("d")


def test_resolve_pose(registry):
    store = registry.location_store("hq")
    store.create("goal", "floor-1", 3.5, 4.5, 1.0)
    map_id, pose = store.resolve_pose("goal")
    assert map_id == "floor-1"
    assert (pose.x, pose.y, pose.theta) == (3.5, 4.5, 1.0)


def test_persistence_reload(tmp_path):
    r1 = SiteRegistry(tmp_path)
    r1.create_site("hq", "본사")
    r1.map_store("hq").ingest_upload("floor-1", _vendor_files(), activate=True)
    r1.location_store("hq").create("keep", "floor-1", 1.0, 2.0, 0.5)

    r2 = SiteRegistry(tmp_path)  # 재시작 시뮬레이션
    loc = r2.location_store("hq").get("keep")
    assert (loc.x, loc.y, loc.theta) == (1.0, 2.0, 0.5)


def test_name_traversal_blocked(registry):
    store = registry.location_store("hq")
    with pytest.raises(LocationError):
        store.create("../evil", "floor-1", 1.0, 2.0)


def test_delete_site_removes_locations(tmp_path):
    r = SiteRegistry(tmp_path)
    r.create_site("hq", "본사")
    r.map_store("hq").ingest_upload("floor-1", _vendor_files(), activate=True)
    r.location_store("hq").create("l", "floor-1", 1.0, 2.0)
    r.delete_site("hq", force=True)
    assert not (tmp_path / "sites" / "hq").exists()
