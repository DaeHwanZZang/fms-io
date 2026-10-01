"""MQTT 메시지 -> Fleet 반영 경로 테스트. 실제 브로커 없이 콜백만 직접 때린다."""

from __future__ import annotations

from types import SimpleNamespace

from common.schemas import ConnectionState, RobotState, topic_connection, topic_state
from fms_server.config import Settings
from fms_server.fleet import Fleet
from fms_server.mqtt_client import FmsMqttClient, _parse_topic
from fms_server.tests.factories import make_connection, make_state


def _msg(topic: str, payload: str):
    return SimpleNamespace(topic=topic, payload=payload.encode("utf-8"))


def _client() -> tuple[FmsMqttClient, Fleet]:
    fleet = Fleet(stale_after=3.0)
    return FmsMqttClient(fleet, Settings()), fleet


def test_parse_topic():
    assert _parse_topic("fms/v1/AMR-1/state") == ("AMR-1", "state")
    assert _parse_topic("fms/v1/AMR-1/connection") == ("AMR-1", "connection")
    assert _parse_topic("fms/v1/AMR-1/order") == (None, None)
    assert _parse_topic("garbage") == (None, None)


def test_valid_state_lands_in_fleet():
    client, fleet = _client()
    state = make_state("AMR-001", RobotState.IDLE)
    client._on_message(None, None, _msg(topic_state("AMR-001"), state.model_dump_json()))

    rec = fleet.get("AMR-001")
    assert rec is not None and rec.state.state == RobotState.IDLE


def test_valid_connection_lands_in_fleet():
    client, fleet = _client()
    conn = make_connection("AMR-002", ConnectionState.ONLINE)
    client._on_message(None, None, _msg(topic_connection("AMR-002"), conn.model_dump_json()))
    assert fleet.get("AMR-002").connection == ConnectionState.ONLINE


def test_broken_json_is_dropped_not_raised():
    client, fleet = _client()
    client._on_message(None, None, _msg(topic_state("AMR-003"), "{not json"))
    assert fleet.get("AMR-003") is None  # 서버 안 죽고, 그냥 무시


def test_topic_payload_robot_id_mismatch_rejected():
    client, fleet = _client()
    state = make_state("AMR-OTHER")
    client._on_message(None, None, _msg(topic_state("AMR-004"), state.model_dump_json()))
    assert fleet.get("AMR-004") is None
    assert fleet.get("AMR-OTHER") is None


def test_empty_connection_payload_ignored():
    client, fleet = _client()
    client._on_message(None, None, _msg(topic_connection("AMR-005"), ""))
    assert fleet.get("AMR-005") is None


def test_unknown_topic_ignored():
    client, fleet = _client()
    client._on_message(None, None, _msg("fms/v1/AMR-006/order", "{}"))
    assert fleet.all() == []
