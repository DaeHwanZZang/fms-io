"""Site / Map / Robot REST API 테스트."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common.schemas import ConnectionState, RobotState
from fms_server.main import app
from fms_server.sites import SiteRegistry
from fms_server.tests.factories import make_state

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")


@pytest.fixture
def client(tmp_path):
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        app.state.sites = registry
        # 코디네이터/브로드캐스터는 lifespan 에서 registry 를 캡처했으므로 같이 갈아끼운다
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        yield c


def _files():
    return [
        ("files", (p.name, p.read_bytes(), "application/octet-stream"))
        for p in VENDOR.iterdir()
        if p.is_file()
    ]


def _make_site_with_map(client, site_id="hq", map_id="floor-1", activate=True):
    client.post("/api/v1/sites", json={"site_id": site_id, "name": "본사"})
    r = client.post(
        f"/api/v1/sites/{site_id}/maps",
        data={"map_id": map_id, "activate": str(activate).lower()},
        files=_files(),
    )
    assert r.status_code == 200, r.text
    return r.json()


# =============================================================================
# Site
# =============================================================================

def test_create_and_get_site(client):
    r = client.post("/api/v1/sites", json={"site_id": "hq", "name": "본사"})
    assert r.status_code == 200
    assert r.json()["site_id"] == "hq"

    dup = client.post("/api/v1/sites", json={"site_id": "hq", "name": "x"})
    assert dup.status_code == 409

    detail = client.get("/api/v1/sites/hq").json()
    assert detail["name"] == "본사"
    assert detail["maps"] == []
    assert detail["robots"] == []


def test_get_unknown_site_404(client):
    assert client.get("/api/v1/sites/nope").status_code == 404


# =============================================================================
# Map
# =============================================================================

def test_upload_map_to_site(client):
    body = _make_site_with_map(client)
    assert body["site_id"] == "hq"
    assert body["map_id"] == "floor-1"
    assert body["created"] is True
    assert any("prohibit zone" in w for w in body["warnings"])

    maps = client.get("/api/v1/sites/hq/maps").json()
    assert maps[0]["map_id"] == "floor-1"
    assert maps[0]["active_version"] == body["version"]


def test_upload_to_missing_site_404(client):
    r = client.post(
        "/api/v1/sites/ghost/maps", data={"map_id": "floor-1"}, files=_files()
    )
    assert r.status_code == 404


def test_bundle_and_manifest(client):
    body = _make_site_with_map(client)
    version = body["version"]

    manifest = client.get("/api/v1/sites/hq/maps/floor-1/manifest").json()
    assert manifest["version"] == version

    bundle = client.get("/api/v1/sites/hq/maps/floor-1/bundle")
    assert bundle.status_code == 200
    assert bundle.headers["x-map-version"] == version
    zf = zipfile.ZipFile(io.BytesIO(bundle.content))
    assert set(zf.namelist()) == {
        "fms_map.json", "navi_gridmap.baked.png", "annotations.json", "manifest.json"
    }


def test_two_maps_per_site(client):
    client.post("/api/v1/sites", json={"site_id": "hq", "name": "본사"})
    for mid, dsp in (("floor-1", "0"), ("floor-2", "2")):
        r = client.post(
            f"/api/v1/sites/hq/maps",
            data={"map_id": mid, "despeckle_iterations": dsp, "activate": "true"},
            files=_files(),
        )
        assert r.status_code == 200
    maps = {m["map_id"] for m in client.get("/api/v1/sites/hq/maps").json()}
    assert maps == {"floor-1", "floor-2"}


# =============================================================================
# Robot 등록 + SET_MAP 타겟팅
# =============================================================================

def test_register_robot_and_becomes_available(client):
    body = _make_site_with_map(client)
    version = body["version"]

    fleet = app.state.fleet
    # 로봇이 배정 맵의 활성 버전을 이미 들고 접속했다고 치자
    fleet.apply_state(make_state("AMR-001", RobotState.IDLE, map_id="floor-1", map_version=version))
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)
    try:
        # 등록 전: UNREGISTERED
        v = client.get("/api/v1/robots/AMR-001").json()
        assert v["liveness"] == "UNREGISTERED" and v["available_for_job"] is False

        # 등록
        reg = client.post(
            "/api/v1/sites/hq/robots", json={"robot_id": "AMR-001", "map_id": "floor-1"}
        )
        assert reg.status_code == 200
        assert reg.json()["assignment"]["map_id"] == "floor-1"

        # 등록 후: ONLINE + map_synced + available
        v = client.get("/api/v1/robots/AMR-001").json()
        assert v["registered"] is True
        assert v["liveness"] == "ONLINE"
        assert v["map_synced"] is True
        assert v["available_for_job"] is True
        assert v["site_id"] == "hq"
    finally:
        fleet.forget("AMR-001")


def test_set_map_carries_uid_not_admin_label(client, monkeypatch):
    """SET_MAP 의 map_id params 는 관리자 라벨(floor-1)이 아니라 map_uid 여야 한다."""
    from fms_server.mapkit.store import compute_map_uid

    sent = []
    monkeypatch.setattr(app.state.mqtt, "publish_instant", lambda a: sent.append(a))

    body = _make_site_with_map(client)  # hq / floor-1
    fleet = app.state.fleet
    fleet.apply_state(make_state("AMR-U", RobotState.IDLE))
    fleet.apply_connection("AMR-U", ConnectionState.ONLINE)
    try:
        client.post("/api/v1/sites/hq/robots", json={"robot_id": "AMR-U", "map_id": "floor-1"})
        assert sent, "SET_MAP 이 발행되지 않았다"
        params = sent[-1].params
        uid = compute_map_uid("hq", "floor-1")
        assert params["map_id"] == uid
        assert params["map_id"] != "floor-1"
        assert params["map_version"] == body["version"]
        assert "/sites/hq/maps/floor-1/bundle" in params["url"]  # url 은 사람이 읽는 라우트
    finally:
        fleet.forget("AMR-U")


def test_map_uid_in_responses(client):
    from fms_server.mapkit.store import compute_map_uid

    body = _make_site_with_map(client)
    uid = compute_map_uid("hq", "floor-1")
    assert body["map_uid"] == uid

    m = client.get("/api/v1/sites/hq/maps/floor-1").json()
    assert m["map_uid"] == uid

    bundle = client.get("/api/v1/sites/hq/maps/floor-1/bundle")
    zf = zipfile.ZipFile(io.BytesIO(bundle.content))
    import json as _json
    fms_map = _json.loads(zf.read("fms_map.json"))
    assert fms_map["name"] == uid


def test_register_robot_unknown_map_422(client):
    client.post("/api/v1/sites", json={"site_id": "hq", "name": "본사"})
    r = client.post(
        "/api/v1/sites/hq/robots", json={"robot_id": "AMR-001", "map_id": "ghost"}
    )
    assert r.status_code == 422


def test_robot_with_stale_map_not_available(client):
    body = _make_site_with_map(client)

    fleet = app.state.fleet
    # 로봇이 옛 버전을 들고 있음
    fleet.apply_state(make_state("AMR-002", RobotState.IDLE, map_id="floor-1", map_version="old000000000"))
    fleet.apply_connection("AMR-002", ConnectionState.ONLINE)
    try:
        client.post(
            "/api/v1/sites/hq/robots", json={"robot_id": "AMR-002", "map_id": "floor-1"}
        )
        v = client.get("/api/v1/robots/AMR-002").json()
        assert v["registered"] is True
        assert v["map_synced"] is False
        assert v["available_for_job"] is False  # 맵 버전 불일치
    finally:
        fleet.forget("AMR-002")


def test_reassign_and_unregister(client):
    client.post("/api/v1/sites", json={"site_id": "hq", "name": "본사"})
    for mid, dsp in (("floor-1", "0"), ("floor-2", "2")):
        client.post(
            "/api/v1/sites/hq/maps",
            data={"map_id": mid, "despeckle_iterations": dsp, "activate": "true"},
            files=_files(),
        )
    client.post("/api/v1/sites/hq/robots", json={"robot_id": "AMR-003", "map_id": "floor-1"})

    put = client.put("/api/v1/sites/hq/robots/AMR-003", json={"map_id": "floor-2"})
    assert put.status_code == 200
    assert put.json()["assignment"]["map_id"] == "floor-2"

    dele = client.delete("/api/v1/sites/hq/robots/AMR-003")
    assert dele.status_code == 200
    assert client.get("/api/v1/sites/hq/robots").json() == []


def test_delete_site_with_robots_conflict(client):
    _make_site_with_map(client)
    client.post("/api/v1/sites/hq/robots", json={"robot_id": "AMR-9", "map_id": "floor-1"})
    assert client.delete("/api/v1/sites/hq").status_code == 409
    assert client.delete("/api/v1/sites/hq?force=true").status_code == 200
