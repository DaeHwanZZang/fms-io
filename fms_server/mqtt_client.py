"""
FMS <-> 로봇 MQTT 통신
======================

구독:
    fms/v1/+/state        <- 로봇 주기 상태 (QoS 0)
    fms/v1/+/connection   <- 로봇 접속 상태 (QoS 1, retain). LWT 로 브로커가 대신 발행

발행:
    fms/v1/{robot_id}/order      -> 작업 지시서 (QoS 1)
    fms/v1/{robot_id}/instant    -> 즉시 명령 (QoS 1)

수신 메시지는 `common/schemas.py` 로 검증 후 `Fleet` 에 반영한다. 파싱 실패는
로깅하고 버린다 — 로봇 하나가 깨진 JSON 을 보내도 서버는 계속 돈다.

paho-mqtt 2.x API (`CallbackAPIVersion.VERSION2`). 네트워크 루프는 `loop_start()`
로 백그라운드 스레드에서 돈다. 브로커가 없어도 `start()` 는 성공하고, paho 가
`reconnect_delay_set` 규칙으로 재연결을 시도한다.
"""

from __future__ import annotations

import logging
from typing import Optional

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from common.schemas import (
    TOPIC_CONNECTION_ALL,
    TOPIC_STATE_ALL,
    Connection,
    ConnectionState,
    InstantAction,
    Order,
    State,
    topic_instant,
    topic_order,
)
from fms_server.config import Settings
from fms_server.fleet import Fleet

log = logging.getLogger("fms.mqtt")


class FmsMqttClient:
    def __init__(self, fleet: Fleet, settings: Settings) -> None:
        self._fleet = fleet
        self._settings = settings
        self._connected = False

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            clean_session=True,
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # -- 수명 주기 --------------------------------------------------------

    def start(self) -> None:
        host, port = self._settings.mqtt_host, self._settings.mqtt_port
        try:
            # connect_async + loop_start: 브로커가 아직 없어도 블로킹하지 않는다.
            self._client.connect_async(host, port, self._settings.mqtt_keepalive)
            self._client.loop_start()
            log.info("MQTT 루프 시작 (%s:%d)", host, port)
        except Exception:
            if self._settings.mqtt_required:
                raise
            log.exception("MQTT 시작 실패 — 서버는 계속 뜬다 (재연결은 paho 담당)")

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
        log.info("MQTT 루프 중지")

    @property
    def connected(self) -> bool:
        return self._connected

    # -- 발행 ------------------------------------------------------------

    def publish_order(self, order: Order) -> None:
        topic = topic_order(order.robot_id)
        payload = order.model_dump_json()
        info = self._client.publish(topic, payload, qos=1)
        log.info("order 발행 %s (order_id=%s update=%d rc=%s)",
                 topic, order.order_id, order.order_update_id, info.rc)

    def publish_instant(self, action: InstantAction) -> None:
        topic = topic_instant(action.robot_id)
        payload = action.model_dump_json()
        info = self._client.publish(topic, payload, qos=1)
        log.info("instant 발행 %s (%s rc=%s)", topic, action.action_type, info.rc)

    # -- 콜백 ----------------------------------------------------------

    def _on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.warning("MQTT 연결 거부: %s", reason_code)
            return
        self._connected = True
        client.subscribe([(TOPIC_STATE_ALL, 0), (TOPIC_CONNECTION_ALL, 1)])
        log.info("MQTT 연결됨, 구독: %s, %s", TOPIC_STATE_ALL, TOPIC_CONNECTION_ALL)

    def _on_disconnect(self, client: mqtt.Client, userdata, *args) -> None:
        self._connected = False
        log.warning("MQTT 연결 끊김 — 재연결 시도 중")

    def _on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        robot_id, kind = _parse_topic(msg.topic)
        if robot_id is None:
            log.debug("알 수 없는 토픽 무시: %s", msg.topic)
            return
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("%s: UTF-8 아님, 버린다", msg.topic)
            return

        if kind == "state":
            self._handle_state(robot_id, payload)
        elif kind == "connection":
            self._handle_connection(robot_id, payload)

    def _handle_state(self, robot_id: str, payload: str) -> None:
        try:
            state = State.model_validate_json(payload)
        except ValidationError as exc:
            log.warning("%s state 검증 실패: %s", robot_id, _short(exc))
            return
        if state.robot_id != robot_id:
            log.warning("토픽 robot_id(%s) != 페이로드(%s), 페이로드 무시", robot_id, state.robot_id)
            return
        self._fleet.apply_state(state)

    def _handle_connection(self, robot_id: str, payload: str) -> None:
        if not payload.strip():
            # retain 지우기(빈 페이로드)로 오는 경우.
            log.info("%s connection retain 삭제됨", robot_id)
            return
        try:
            connection = Connection.model_validate_json(payload)
        except ValidationError as exc:
            log.warning("%s connection 검증 실패: %s", robot_id, _short(exc))
            return
        self._fleet.apply_connection(robot_id, connection.connection_state)


# =============================================================================
# 헬퍼
# =============================================================================

def _parse_topic(topic: str) -> tuple[Optional[str], Optional[str]]:
    """fms/v1/{robot_id}/{kind} -> (robot_id, kind). 아니면 (None, None)."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "fms" or parts[1] != "v1":
        return None, None
    _, _, robot_id, kind = parts
    if kind not in ("state", "connection"):
        return None, None
    return robot_id, kind


def _short(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', '?')} (외 {len(errors) - 1}건)"


__all__ = ["FmsMqttClient", "ConnectionState"]
