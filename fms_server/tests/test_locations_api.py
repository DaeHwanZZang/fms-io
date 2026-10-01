"""Named location REST API 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fms_server.main import app
from fms_server.sites import SiteRegistry

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")


@pytest.fixture
def client(tmp_path):
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        yield c


def _files():
    return [
        ("files", (p.name, p.read_bytes(), "application/octet-stream"))
        for p in VENDOR.iterdir()
        if p.is_file()
    ]


@pytest.fixture
def site(client):
    client.post("/api/v1/sites", json={"site_id": "hq", "name": "본사"})
    r = client.post(
        "/api/v1/sites/hq/maps",
        data={"map_id": "floor-1", "activate": "true"},
        files=_files(),
    )
    assert r.status_code == 200, r.text
    return client


def test_crud_roundtrip(site):
    c = site
    r = c.post(
        "/api/v1/sites/hq/locations",
        json={"name": "충전소-A", "map_id": "floor-1", "x": 1.0, "y": 2.0, "type": "charger"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "충전소-A"
    assert body["theta"] == 0.0
    assert body["type"] == "charger"
    assert len(body["map_uid"]) == 24

    assert c.get("/api/v1/sites/hq/locations").json()[0]["name"] == "충전소-A"
    assert c.get("/api/v1/sites/hq/locations/충전소-A").json()["x"] == 1.0

    put = c.put("/api/v1/sites/hq/locations/충전소-A", json={"x": 9.0})
    assert put.status_code == 200
    assert put.json()["x"] == 9.0 and put.json()["y"] == 2.0

    assert c.delete("/api/v1/sites/hq/locations/충전소-A").status_code == 200
    assert c.get("/api/v1/sites/hq/locations").json() == []


def test_duplicate_name_422(site):
    site.post("/api/v1/sites/hq/locations", json={"name": "d", "map_id": "floor-1", "x": 1, "y": 2})
    dup = site.post("/api/v1/sites/hq/locations", json={"name": "d", "map_id": "floor-1", "x": 3, "y": 4})
    assert dup.status_code == 422


def test_unknown_map_422(site):
    r = site.post(
        "/api/v1/sites/hq/locations",
        json={"name": "x", "map_id": "ghost", "x": 1, "y": 2},
    )
    assert r.status_code == 422


def test_unknown_site_404(client):
    assert client.get("/api/v1/sites/nope/locations").status_code == 404


def test_get_missing_location_404(site):
    assert site.get("/api/v1/sites/hq/locations/nope").status_code == 404


def test_out_of_bounds_coord_warns_not_rejects(site):
    r = site.post(
        "/api/v1/sites/hq/locations",
        json={"name": "far", "map_id": "floor-1", "x": 99999.0, "y": 99999.0},
    )
    assert r.status_code == 200
    assert any("맵 범위 밖" in w for w in r.json()["warnings"])
