"""io_logic / mockgpio 단위 테스트. 실행: cd fms-io/server && ../.venv/bin/python -m pytest"""

import io_logic as L
from mockgpio import GPIO


def test_level_conversion_roundtrip():
    for al in (True, False):
        for v in (True, False):
            assert L.to_logical(L.to_level(v, al), al) == v
    assert L.to_level(True, True) == 0      # 풀업 버튼: 눌림 = LOW


def test_flag_values():
    f = L.flag_values({"button": True}, {"buzzer": False, "lamp_red": False})
    assert f == {"button": True, "buzzer": False, "lamp_red": False}


def test_mockgpio_edge_callback():
    GPIO.cleanup()
    hits = []
    GPIO.setup(5, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    assert GPIO.input(5) == 1
    GPIO.add_event_detect(5, GPIO.FALLING, callback=hits.append)
    GPIO.set_input(5, 0)
    GPIO.set_input(5, 0)     # 같은 값은 엣지 아님
    GPIO.set_input(5, 1)     # 상승은 FALLING 감지 대상 아님
    assert hits == [5]
    GPIO.cleanup()


def test_no_automatic_rules_and_lamp_one_at_a_time():
    """파이는 스스로 출력을 바꾸지 않는다. 출력은 apply_output(FMS/대시보드)으로만 바뀐다."""
    import io_server as s
    s.GPIO.cleanup()
    s.init_gpio()
    assert not any(s._read(n) for n in ("lamp_red", "lamp_yellow", "lamp_green", "buzzer"))   # 시작 시 전부 꺼짐
    s._world["button"] = True
    s.GPIO.set_input(5, 0)                                         # 버튼 눌림 -> 출력 변화 없음
    assert not any(s._read(n) for n in ("lamp_red", "lamp_yellow", "lamp_green", "buzzer"))
    assert s.apply_output("lamp_red", True, from_fms=True) is None
    assert s._read("lamp_red")
    assert s.apply_output("lamp_yellow", True, from_fms=True) is None   # 한 번에 한 색만
    assert s._read("lamp_yellow") and not s._read("lamp_red")
    assert s.apply_output("lamp_red", False) is None                    # 다른 색 끄기는 켜진 색을 건드리지 않음
    assert s._read("lamp_yellow")
    assert s.apply_output("buzzer", True, from_fms=True) is None and s._read("buzzer")
    assert s.apply_output("lamp_auto", True, from_fms=True) is not None  # 자동 모드는 없다
    assert s.apply_output("button", True, from_fms=True) is not None     # 입력 핀은 제어 불가
    s.GPIO.cleanup()


def test_mock_fallback_on_real_pi_is_refused(monkeypatch):
    """라즈베리파이 위에서 RPi.GPIO 를 못 불러와 mock 으로 떨어지면 시작을 거부한다 (조용히 mock 금지)."""
    import io_server as s
    monkeypatch.setattr(s, "running_on_pi", lambda: True)
    monkeypatch.delenv("IO_ALLOW_MOCK", raising=False)
    assert s.IS_MOCK and "mock" in s.gpio_backend_problem()
    monkeypatch.setenv("IO_ALLOW_MOCK", "1")
    assert s.gpio_backend_problem() is None
    monkeypatch.setattr(s, "running_on_pi", lambda: False)
    monkeypatch.delenv("IO_ALLOW_MOCK", raising=False)
    assert s.gpio_backend_problem() is None                       # 맥/윈도우 개발 환경은 그대로 mock


def test_uplink_survives_provider_error(monkeypatch):
    """GPIO 읽기 예외가 나도 업로드 스레드는 죽지 않고 다음 주기에 재시도한다."""
    import io_config
    from fms_uplink import FmsUplink
    calls = []

    def provider():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("GPIO busy")
        return {}

    monkeypatch.setattr(io_config, "UPLINK_INTERVAL_SEC", 0.05)
    up = FmsUplink(provider=provider, on_status=lambda: None)
    up.start()
    import time
    time.sleep(0.3)
    up.stop()
    assert len(calls) >= 2   # 첫 라운드 예외 뒤에도 계속 돈다
