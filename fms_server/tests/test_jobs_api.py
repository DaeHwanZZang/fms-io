"""job REST API 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common.schemas import ConnectionState, RobotState
from fms_server.main import app
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobStore
from fms_server.tests.factories import make_state

VENDOR = Path("AMR_client/maps/641931de9eae7cecb34d5765")


def _files():
    return [
        ("files", (p.name, p.read_bytes(), "application/octet-stream"))
        for p in VENDOR.iterdir()
        if p.is_file()
    ]


@pytest.fixture
def client(tmp_path):
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        store = JobStore(tmp_path)
        app.state.sites = registry
        app.state.jobs = store
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        app.state.job_coordinator._registry = registry
        app.state.job_coordinator._store = store
        yield c


@pytest.fixture
def ready(client):
    """hq / floor-1(active) / AMR-001 등록·online·맵동기 / location 'dest'."""
    client.post("/api/v1/sites", json={"site_id": "hq", "name": "본사"})
    r = client.post(
        "/api/v1/sites/hq/maps",
        data={"map_id": "floor-1", "activate": "true"},
        files=_files(),
    )
    version = r.json()["version"]
    client.post("/api/v1/sites/hq/robots", json={"robot_id": "AMR-001", "map_id": "floor-1"})
    client.post(
        "/api/v1/sites/hq/locations",
        json={"name": "dest", "map_id": "floor-1", "x": 3.0, "y": 4.0},
    )
    fleet = app.state.fleet
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)
    fleet.apply_state(
        make_state("AMR-001", RobotState.IDLE, map_id="uid", map_version=version)
    )
    yield client
    fleet.forget("AMR-001")


def test_create_list_get(ready):
    c = ready
    r = c.post(
        "/api/v1/sites/hq/jobs",
        json={"commands": [{"type": "move", "target": "dest"}], "robot_id": "AMR-001"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "pending"
    assert r.json()["total_commands"] == 1

    assert c.get("/api/v1/sites/hq/jobs").json()[0]["job_id"] == job_id
    assert c.get(f"/api/v1/sites/hq/jobs/{job_id}").json()["status"] == "pending"


def test_dispatch_and_cancel(ready):
    c = ready
    job_id = c.post(
        "/api/v1/sites/hq/jobs",
        json={"commands": [{"type": "move", "target": "dest"}], "robot_id": "AMR-001"},
    ).json()["job_id"]

    d = c.post(f"/api/v1/sites/hq/jobs/{job_id}/dispatch", json={})
    assert d.status_code == 200, d.text
    assert d.json()["status"] == "working"
    assert d.json()["order_id"] == job_id

    cancel = c.post(f"/api/v1/sites/hq/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "canceled"


def test_dispatch_unknown_location_422(ready):
    c = ready
    job_id = c.post(
        "/api/v1/sites/hq/jobs",
        json={"commands": [{"type": "move", "target": "ghost"}], "robot_id": "AMR-001"},
    ).json()["job_id"]
    d = c.post(f"/api/v1/sites/hq/jobs/{job_id}/dispatch", json={})
    assert d.status_code == 422


def test_create_bad_command_422(ready):
    r = ready.post(
        "/api/v1/sites/hq/jobs",
        json={"commands": [{"type": "charging", "target": "dest"}], "robot_id": "AMR-001"},
    )
    assert r.status_code == 422


def test_job_unknown_site_404(client):
    assert client.get("/api/v1/sites/nope/jobs").status_code == 404


def test_job_wrong_site_404(ready):
    ready.post("/api/v1/sites", json={"site_id": "other", "name": "x"})
    job_id = ready.post(
        "/api/v1/sites/hq/jobs",
        json={"commands": [{"type": "move", "target": "dest"}]},
    ).json()["job_id"]
    assert ready.get(f"/api/v1/sites/other/jobs/{job_id}").status_code == 404


def test_delete_working_job_422(ready):
    c = ready
    job_id = c.post(
        "/api/v1/sites/hq/jobs",
        json={"commands": [{"type": "move", "target": "dest"}], "robot_id": "AMR-001"},
    ).json()["job_id"]
    c.post(f"/api/v1/sites/hq/jobs/{job_id}/dispatch", json={})
    assert c.delete(f"/api/v1/sites/hq/jobs/{job_id}").status_code == 422
    c.post(f"/api/v1/sites/hq/jobs/{job_id}/cancel")
    assert c.delete(f"/api/v1/sites/hq/jobs/{job_id}").status_code == 200
