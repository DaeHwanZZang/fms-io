"""로봇에 이동 job 을 보내 대시보드에서 움직임을 확인하는 개발용 도구.

    # 디렉터리: fms-io/
    .venv/bin/python tools/move_robot.py WP1 WP2 [--robot AMR-001] [--site sited_test0920]
    .venv/bin/python tools/move_robot.py --add WP3 3.0 -0.4       # named location 추가

FMS REST(/api/v1/sites/.../jobs)만 쓴다. 로봇이 FATAL 오류(예: NO_PATH) 상태면 job 을 받지 못하므로
--reset 으로 RESET_ERROR instant 를 MQTT 로 먼저 보낸다.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

FMS = "http://localhost:8000/api/v1"


def call(method, path, body=None):
    req = urllib.request.Request(FMS + path, json.dumps(body).encode() if body is not None else None,
                                 {"content-type": "application/json"}, method=method)
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} {path}: {e.read().decode()}")


def reset_error(robot, host, port):
    import paho.mqtt.client as mqtt
    from common.schemas import InstantAction, InstantActionType, topic_instant
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="homework-move-tool")
    c.connect(host, port)
    c.loop_start()
    now = time.time()
    msg = InstantAction(header_id=0, timestamp=now, robot_id=robot, action_id=f"RESET-{int(now * 1000)}",
                        action_type=InstantActionType.RESET_ERROR)
    c.publish(topic_instant(robot), msg.model_dump_json(), qos=1).wait_for_publish()
    time.sleep(1.5)
    c.loop_stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*", help="이동할 named location 이름들(순서대로)")
    ap.add_argument("--robot", default="AMR-001")
    ap.add_argument("--site", default="sited_test0920")
    ap.add_argument("--map", default="test0920")
    ap.add_argument("--add", nargs=3, metavar=("NAME", "X", "Y"), help="location 추가 후 종료")
    ap.add_argument("--reset", action="store_true", help="이동 전에 RESET_ERROR 전송")
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    a = ap.parse_args()
    site = f"/sites/{a.site}"

    if a.add:
        name, x, y = a.add
        print(call("POST", f"{site}/locations", {"name": name, "map_id": a.map, "x": float(x), "y": float(y), "theta": 0}))
        return
    if not a.targets:
        print("locations:", [(l["name"], l["x"], l["y"]) for l in call("GET", f"{site}/locations")])
        return
    if a.reset:
        reset_error(a.robot, a.mqtt_host, a.mqtt_port)
    job = call("POST", f"{site}/jobs", {"commands": [{"type": "move", "target": t} for t in a.targets], "robot_id": a.robot})
    job = call("POST", f"{site}/jobs/{job['job_id']}/dispatch", {"robot_id": a.robot})
    print("dispatched", job["job_id"], job["status"])
    while True:
        time.sleep(1)
        s = call("GET", f"/robots/{a.robot}")["state"]
        job = call("GET", f"{site}/jobs/{job['job_id']}")
        print(f"{s['state']}/{s['sub_state']} ({s['pose']['x']:.2f}, {s['pose']['y']:.2f}) job={job['status']} {job['done_commands']}/{job['total_commands']}")
        if job["status"] not in ("pending", "working"):
            break


if __name__ == "__main__":
    main()
