"""
라즈베리파이 시뮬레이션 서버 (Flask-SocketIO, 포트 5001)

    cd fms-io && .venv/bin/python server/io_server.py

- 핀 배치/장치는 io_config.py. GPIO 는 RPi.GPIO 가 없으면 mockgpio.
- 대시보드(web/pi.html)가 sim_input 으로 "현장"(버튼 누름)을 바꾸면
  mock 입력 핀 레벨이 바뀌고 엣지 콜백 경로를 그대로 탄다.
- 센서 상태는 FMS 로 업로드된다 (fms_uplink.py -> PUT /api/v1/flags/{name}).
  FMS job 의 wait_flag 명령이 이 값을 읽는다.

Socket.IO
    C->S  get_io_status | sim_input {name:"button", value} |
          output_control {name, value}
    HTTP  POST /api/output {name, value}   FMS job 의 set_output 명령 (파이에는 자동 규칙이 없다)
    S->C  io_status | io_event {ts,type,name,value,message} | log_history
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask
from flask_socketio import SocketIO

_GPIO_IMPORT_ERROR = None   # 실물 GPIO 를 못 불러온 이유 (mock 으로 떨어졌을 때 진단용)
try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError) as exc:
    _GPIO_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    from mockgpio import GPIO

import io_config as cfg
import io_logic as logic
from fms_uplink import FmsUplink

log = logging.getLogger("io.server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

IS_MOCK = hasattr(GPIO, "set_input")

_lock = threading.RLock()
_world = {"button": False}                      # 현장 상태 (mock 에서만 바꿀 수 있다)
_events: deque = deque(maxlen=cfg.LOG_MAX)


# -- 핀 읽기/쓰기 (논리값 기준) ------------------------------------------

def _read(name: str) -> bool:
    d = cfg.DEVICE_BY_NAME[name]
    return logic.to_logical(GPIO.input(d["bcm"]), d["active_low"])


def _write(name: str, logical: bool) -> None:
    d = cfg.DEVICE_BY_NAME[name]
    GPIO.output(d["bcm"], logic.to_level(logical, d["active_low"]))


def _inputs() -> dict[str, bool]:
    return {d["name"]: _read(d["name"]) for d in cfg.DEVICES if d["dir"] == "in"}


def _outputs() -> dict[str, bool]:
    return {d["name"]: _read(d["name"]) for d in cfg.DEVICES if d["dir"] == "out"}


def _flags() -> dict[str, bool]:
    with _lock:
        return logic.flag_values(_inputs(), _outputs())


def _log(type_: str, name: str, value, message: str) -> None:
    ev = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
          "type": type_, "name": name, "value": value, "message": message}
    _events.append(ev)
    socketio.emit("io_event", ev)


# -- 상태 --------------------------------------------------------------

def _status() -> dict:
    with _lock:
        devices = []
        for d in cfg.DEVICES:
            level = GPIO.input(d["bcm"])
            devices.append({**d, "level": level, "logical": int(logic.to_logical(level, d["active_low"]))})
        return {
            "mock": IS_MOCK,
            "world": dict(_world),
            "devices": devices,
            "flags": _flags(),
            "flag_info": cfg.FLAGS,
            "uplink": uplink.status(),
        }


def _emit_status() -> None:
    socketio.emit("io_status", _status())


def _settle() -> None:
    """입력/출력이 바뀐 뒤: 대시보드 갱신 -> FMS 업로드. 파이는 스스로 출력을 바꾸지 않는다 (제어는 전부 FMS)."""
    _emit_status()
    uplink.mark_dirty()


def _on_edge(pin: int) -> None:
    d = next(x for x in cfg.DEVICES if x["bcm"] == pin)
    logical = _read(d["name"])
    if d["kind"] == "button":
        _log("input", d["name"], int(logical), "Push button pressed" if logical else "Push button released")
    _settle()


uplink = FmsUplink(provider=_flags, on_status=_emit_status)


def running_on_pi() -> bool:
    try:
        return "raspberry pi" in Path("/proc/device-tree/model").read_text(errors="ignore").lower()
    except OSError:
        return False


def gpio_backend_problem() -> str | None:
    """
    라즈베리파이 위인데 실물 GPIO 를 못 불러와 mock 으로 떨어졌으면 그 사유. 아니면 None.
    조용히 mock 으로 돌면 화면은 멀쩡한데 핀이 하나도 안 움직여서 원인을 찾기 어렵다.
    """
    if not IS_MOCK or not running_on_pi() or os.environ.get("IO_ALLOW_MOCK") == "1":
        return None
    return (
        f"라즈베리파이인데 실물 GPIO 라이브러리를 불러오지 못해 mock 으로 떨어졌다 ({_GPIO_IMPORT_ERROR}). "
        "pip install rpi-lgpio (venv 라면 그 venv 안에서) 또는 apt 패키지를 쓰려면 "
        "python3 -m venv --system-site-packages 로 venv 를 만든다. 일부러 mock 으로 돌리려면 IO_ALLOW_MOCK=1."
    )


# 실물 GPIO 초기화가 실패할 때 흔한 원인 (메시지 일부 -> 안내)
_INIT_HINTS = {
    "Cannot determine SOC peripheral base address": "RPi.GPIO 는 Pi 5 를 지원하지 않는다 -> pip uninstall RPi.GPIO && pip install rpi-lgpio",
    "Failed to add edge detection": "RPi.GPIO 0.7.x 의 엣지 감지는 최신 커널(sysfs GPIO 번호 변경)에서 실패한다 -> rpi-lgpio 사용",
    "No access to /dev/mem": "권한 문제 -> 사용자를 gpio 그룹에 추가 (sudo usermod -aG gpio $USER, 재로그인)",
    "GPIO busy": "다른 프로세스가 핀을 쓰고 있다 -> 이전 io_server 프로세스를 종료",
}

def init_gpio() -> None:
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    for d in cfg.DEVICES:
        if d["dir"] == "out":
            GPIO.setup(d["bcm"], GPIO.OUT, initial=logic.to_level(False, d["active_low"]))   # 출력은 전부 꺼진 채 시작
        else:
            GPIO.setup(d["bcm"], GPIO.IN, pull_up_down=GPIO.PUD_UP if d["active_low"] else GPIO.PUD_OFF)
    for d in cfg.DEVICES:
        if d["dir"] == "in":
            GPIO.add_event_detect(d["bcm"], GPIO.BOTH, callback=_on_edge, bouncetime=20)
    _settle()


# -- Socket.IO ---------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("pi.html")


@socketio.on("connect")
def on_connect():
    socketio.emit("log_history", list(_events), to=None)
    socketio.emit("io_status", _status())


@socketio.on("get_io_status")
def on_get_status():
    socketio.emit("io_status", _status())


def _reject(msg: str) -> None:
    from flask_socketio import emit
    emit("io_event", {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "type": "reject", "name": "", "value": None, "message": msg})


@socketio.on("sim_input")
def on_sim_input(data):
    """현장 시뮬레이션: name = button(눌림)."""
    if not IS_MOCK:
        return _reject("sim_input 은 mock GPIO 에서만 가능하다 (실물 모드)")
    name, value = (data or {}).get("name"), bool((data or {}).get("value"))
    if name not in _world:
        return _reject(f"알 수 없는 sim_input name: {name!r}")
    with _lock:
        if _world[name] == value:
            return
        _world[name] = value
        b = cfg.DEVICE_BY_NAME["button"]
        GPIO.set_input(b["bcm"], logic.to_level(value, b["active_low"]))   # 엣지 콜백 경로
    _settle()


def apply_output(name, value: bool, *, from_fms: bool = False) -> str | None:
    """
    출력 핀 제어. 성공하면 None, 거부하면 사유 문자열. 파이에는 자동 규칙이 없다 —
    출력은 FMS job 의 set_output(POST /api/output) 또는 대시보드 수동 조작으로만 바뀐다.
    경광등은 한 번에 한 색만 켠다: 한 색을 켜면 나머지는 꺼지고, 끄기는 그 색만 끈다.
    """
    d = cfg.DEVICE_BY_NAME.get(name)
    if d is None or d["dir"] != "out":
        return f"출력이 아닌 이름: {name!r}"
    who = "FMS: " if from_fms else ""
    with _lock:
        if d["kind"] == "lamp":
            if value:
                for lamp in cfg.LAMPS:   # 한 번에 한 색만
                    _write(lamp, lamp == name)
            else:
                _write(name, False)
        else:
            _write(name, value)
        _log("output", name, int(value), f"{who}{d['label']} {'ON' if value else 'OFF'}")
    _settle()
    return None


@socketio.on("output_control")
def on_output_control(data):
    """대시보드의 출력 수동 조작(테스트용): buzzer, lamp_*."""
    err = apply_output((data or {}).get("name"), bool((data or {}).get("value")))
    if err:
        _reject(err)


@app.route("/api/output", methods=["POST"])
def api_output():
    """FMS 가 job 시퀀스의 set_output 명령으로 부른다. body: {name, value}. 성공 200, 거부 422."""
    from flask import jsonify, request
    data = request.get_json(silent=True) or {}
    name, value = data.get("name"), data.get("value")
    if not isinstance(name, str) or value not in (0, 1, True, False):
        return jsonify(error="name(str), value(0|1) 필요"), 422
    err = apply_output(name, bool(value), from_fms=True)
    if err:
        return jsonify(error=err), 422
    return jsonify(name=name, value=int(bool(value)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    problem = gpio_backend_problem()
    if problem:
        log.error(problem)
        sys.exit(1)
    if IS_MOCK:
        log.info("mock GPIO 사용 (실물 GPIO 없음: %s)", _GPIO_IMPORT_ERROR)
    try:
        init_gpio()
    except (RuntimeError, ValueError) as exc:
        hint = next((h for k, h in _INIT_HINTS.items() if k in str(exc)), "")
        log.error("GPIO 초기화 실패: %s%s", exc, f"\n  -> {hint}" if hint else "")
        sys.exit(1)
    atexit.register(GPIO.cleanup)   # 실물에서 핀을 정리하지 않으면 다음 실행 때 경고/잔류 레벨이 남는다
    uplink.start()
    log.info("pin map: %s", ", ".join(f"{d['name']}=BCM{d['bcm']}" for d in cfg.DEVICES))
    log.info("FMS uplink -> %s  (mock=%s)", cfg.FMS_URL, IS_MOCK)
    socketio.run(app, host="0.0.0.0", port=cfg.SERVER_PORT, allow_unsafe_werkzeug=True)
