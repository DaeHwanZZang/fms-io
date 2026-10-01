"""SiteRegistry 단위 테스트 (영속화 포함)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fms_server.sites import SiteError, SiteRegistry

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")


def _vendor_files() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in VENDOR.iterdir() if p.is_file()}


@pytest.fixture
def registry(tmp_path) -> SiteRegistry:
    return SiteRegistry(tmp_path)


def test_create_and_list_site(registry):
    registry.create_site("hq", "본사")
    sites = registry.list_sites()
    assert [s.site_id for s in sites] == ["hq"]
    assert registry.get_site("hq").name == "본사"


def test_duplicate_site_rejected(registry):
    registry.create_site("hq", "본사")
    with pytest.raises(SiteError):
        registry.create_site("hq", "다시")


def test_persistence_reload(tmp_path):
    r1 = SiteRegistry(tmp_path)
    r1.create_site("hq", "본사")
    r1.map_store("hq").ingest_upload("floor-1", _vendor_files())
    r1.register_robot("hq", "AMR-001", "floor-1")

    r2 = SiteRegistry(tmp_path)  # 재시작 시뮬레이션
    assert [s.site_id for s in r2.list_sites()] == ["hq"]
    a = r2.assignment_for("AMR-001")
    assert a is not None and a.site_id == "hq" and a.map_id == "floor-1"


def test_register_robot_requires_existing_map(registry):
    registry.create_site("hq", "본사")
    with pytest.raises(SiteError):
        registry.register_robot("hq", "AMR-001", "nonexistent-map")


def test_register_then_reassign(registry):
    registry.create_site("hq", "본사")
    store = registry.map_store("hq")
    store.ingest_upload("floor-1", _vendor_files(), despeckle_iterations=0)
    store.ingest_upload("floor-2", _vendor_files(), despeckle_iterations=2)

    registry.register_robot("hq", "AMR-001", "floor-1")
    assert registry.assignment_for("AMR-001").map_id == "floor-1"

    registry.register_robot("hq", "AMR-001", "floor-2")  # 재배정
    assert registry.assignment_for("AMR-001").map_id == "floor-2"


def test_unregister_robot(registry):
    registry.create_site("hq", "본사")
    registry.map_store("hq").ingest_upload("floor-1", _vendor_files())
    registry.register_robot("hq", "AMR-001", "floor-1")

    registry.unregister_robot("hq", "AMR-001")
    assert registry.assignment_for("AMR-001") is None
    with pytest.raises(SiteError):
        registry.unregister_robot("hq", "AMR-001")


def test_delete_site_blocked_by_robots(registry):
    registry.create_site("hq", "본사")
    registry.map_store("hq").ingest_upload("floor-1", _vendor_files())
    registry.register_robot("hq", "AMR-001", "floor-1")

    with pytest.raises(SiteError):
        registry.delete_site("hq")
    registry.delete_site("hq", force=True)
    assert registry.list_sites() == []


def test_site_id_traversal_blocked(registry):
    with pytest.raises(SiteError):
        registry.create_site("../evil", "x")
    with pytest.raises(SiteError):
        registry.get_site("..")
