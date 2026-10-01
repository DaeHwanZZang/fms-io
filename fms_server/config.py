"""FMS 서버 설정.

환경변수로 덮어쓸 수 있다 (배포/CI 에서). 기본값은 로컬 개발 기준.
pydantic-settings 를 쓰지 않는 이유: 의존성 하나 아끼려고. 필드가 몇 개 안 된다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"환경변수 {key} 가 정수가 아니다: {raw!r}") from None


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"환경변수 {key} 가 실수가 아니다: {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    # MQTT 브로커
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_keepalive: int = 30
    mqtt_client_id: str = "fms-server"

    # FMS 영속 데이터(=맵 저장소) 루트. site 등록 정보 + 맵 번들이 여기 쌓인다.
    #   {data_root}/sites/{site_id}/site.json
    #   {data_root}/sites/{site_id}/maps/{map_id}/{version}/...
    data_root: str = "fms_server/maps"

    # 로봇이 맵을 받으러 올 때 쓸 FMS 의 외부 접근 주소 (SET_MAP 의 url 조립용).
    # 로봇과 다른 PC 에 FMS 가 있으면 그 PC 의 IP 로 바꿔야 한다.
    public_url: str = "http://localhost:8000"

    # state 가 이 시간(초) 넘게 안 오면 로봇을 STALE 로 본다.
    # 로봇 state 주기가 200ms 이므로 3초면 15프레임 유실에 해당한다.
    robot_stale_after: float = 3.0

    # [homework_iot 추가] 라즈베리파이(io_server) 주소. job 의 set_output 명령이 여기로 POST /api/output 한다.
    pi_url: str = "http://127.0.0.1:5001"

    # MQTT 연결 실패해도 서버는 뜬다. 재연결은 paho 가 자동으로 한다.
    mqtt_required: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mqtt_host=_env_str("FMS_MQTT_HOST", cls.mqtt_host),
            mqtt_port=_env_int("FMS_MQTT_PORT", cls.mqtt_port),
            mqtt_keepalive=_env_int("FMS_MQTT_KEEPALIVE", cls.mqtt_keepalive),
            mqtt_client_id=_env_str("FMS_MQTT_CLIENT_ID", cls.mqtt_client_id),
            data_root=_env_str("FMS_DATA_ROOT", cls.data_root),
            public_url=_env_str("FMS_PUBLIC_URL", cls.public_url).rstrip("/"),
            pi_url=_env_str("FMS_PI_URL", cls.pi_url).rstrip("/"),
            robot_stale_after=_env_float("FMS_ROBOT_STALE_AFTER", cls.robot_stale_after),
            mqtt_required=_env_str("FMS_MQTT_REQUIRED", "0") not in ("0", "", "false", "False"),
        )


settings = Settings.from_env()
