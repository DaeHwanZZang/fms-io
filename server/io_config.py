"""
라즈베리파이 시뮬레이션 — 핀 배치와 상수
=========================================

핀 번호·장치 이름은 여기서만 정의하고 다른 파일은 참조한다 (대시보드는 이 내용을
서버가 보내주는 `io_status` 로 그대로 받아 그린다).

논리값 1 = 활성(레이저 수신 / 버튼 눌림 / 램프 켜짐). 실배선의 액티브 로우는 `active_low`
로만 표현한다 — 논리값 <-> 전기 레벨 변환은 io_logic.to_level / to_logical 이 하고,
mockgpio 는 이 플래그를 모른다.
"""

from __future__ import annotations

import os

SERVER_PORT = 5001   # macOS 는 AirPlay Receiver 가 5000 을 쓴다

# FMS 서버 주소. 라즈베리파이가 신호를 올려 보내는 곳 (PUT /api/v1/flags/{name}).
FMS_URL = os.environ.get("FMS_URL", "http://127.0.0.1:8000")
UPLINK_INTERVAL_SEC = 2.0     # 값이 안 바뀌어도 이 주기로 전체 플래그를 다시 보낸다 (FMS 재시작 복구)
UPLINK_TIMEOUT_SEC = 1.5
LOG_MAX = 100

# kind: lamp(경광등) / buzzer(부저) / button(푸시 버튼)
# board = 40핀 헤더의 물리 핀 번호 (BCM 번호와 별개)
DEVICES = [
    {"name": "lamp_red",    "label": "Signal tower - red",    "kind": "lamp", "color": "red",
     "dir": "out", "bcm": 17, "board": 11, "active_low": False},
    {"name": "lamp_yellow", "label": "Signal tower - yellow", "kind": "lamp", "color": "yellow",
     "dir": "out", "bcm": 27, "board": 13, "active_low": False},
    {"name": "lamp_green",  "label": "Signal tower - green",  "kind": "lamp", "color": "green",
     "dir": "out", "bcm": 22, "board": 15, "active_low": False},
    {"name": "buzzer",      "label": "Buzzer",                "kind": "buzzer",
     "dir": "out", "bcm": 23, "board": 16, "active_low": False},
    {"name": "button",      "label": "Push button",           "kind": "button",
     "dir": "in",  "bcm": 5,  "board": 29, "active_low": True},   # 풀업 + GND 연결, 누르면 LOW
]

# FMS 로 올리는 신호 (job 의 wait_flag 가 읽는다). 값이 True 면 "올라가 있음(raised)".
# 원시 상태: 장치 이름 그대로 (raised = 논리값 1). 조건은 FMS 에서 정한다.
_RAISED_MEANS = {"lamp": "lit", "buzzer": "sounding", "button": "pressed"}
FLAGS = {
    d["name"]: f"{d['label']}: raised = {_RAISED_MEANS.get(d['kind'], 'active')}" for d in DEVICES
}

DEVICE_BY_NAME = {d["name"]: d for d in DEVICES}
LAMPS = [d["name"] for d in DEVICES if d["kind"] == "lamp"]

# 실물 이전 체크리스트는 CLAUDE.md "실물 라즈베리파이로 옮길 때" 참고.
