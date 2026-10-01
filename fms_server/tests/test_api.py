"""로봇 REST + WebSocket API 테스트. TestClient 가 lifespan 을 돌린다 (MQTT 는
브로커 없이 뜸 — connect_async 라 블로킹 안 함).

site 저장소는 테스트별 임시 디렉터리로 갈아끼운다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common.schemas import ConnectionState, RobotState
from fms_server.main import app
from fms_server.sites import SiteRegistry
from fms_server.tests.factories import make_state


@pytest.fixture
def client(tmp_path):
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        yield c


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["robots_known"] == 0
    assert body["mqtt_connected"] is False
    assert body["sites"] == 0


def test_robots_empty(client):
    body = client.get("/api/v1/robots").json()
    assert body == {
        "robots": [], "count": 0, "available_count": 0, "unregistered_count": 0,
    }


def test_unregistered_robot_is_observed_not_available(client):
    fleet = app.state.fleet
    fleet.apply_state(make_state("AMR-001", RobotState.IDLE))
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)
    try:
        body = client.get("/api/v1/robots").json()
        assert body["count"] == 1
        assert body["unregistered_count"] == 1
        assert body["available_count"] == 0  # 미등록이라 job 대상 아님
        robot = body["robots"][0]
        assert robot["robot_id"] == "AMR-001"
        assert robot["liveness"] == "UNREGISTERED"
        assert robot["registered"] is False
        assert robot["state"]["state"] == "IDLE"  # state 는 관측됨
    finally:
        fleet.forget("AMR-001")


def test_robot_detail_404(client):
    assert client.get("/api/v1/robots/NOPE").status_code == 404


def test_websocket_snapshot_then_event(client):
    fleet = app.state.fleet
    fleet.apply_state(make_state("AMR-777", RobotState.IDLE))
    try:
        with client.websocket_connect("/api/v1/robots/stream") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert any(r["robot_id"] == "AMR-777" for r in snap["robots"])

            fleet.apply_connection("AMR-777", ConnectionState.ONLINE)
            evt = ws.receive_json()
            assert evt["type"] == "event"
            assert evt["kind"] == "connection"
            assert evt["robot_id"] == "AMR-777"
            assert evt["robot"]["liveness"] == "UNREGISTERED"
    finally:
        fleet.forget("AMR-777")
