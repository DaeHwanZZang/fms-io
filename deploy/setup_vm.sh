#!/usr/bin/env bash
# 리눅스 VM(Ubuntu/Debian, systemd) 시연 배포.
# venv 를 만들고, FMS / 파이 시뮬레이터 / 로봇 클라이언트를 systemd 서비스로 등록해 상시 실행한다.
# MQTT 브로커는 apt 의 mosquitto 시스템 서비스(기본 127.0.0.1:1883, 익명 허용)를 그대로 쓴다.
#
# 전제: 아래 구조로 압축이 풀려 있고, apt 패키지 설치와 AMR_client 빌드(make)가 끝나 있다.
#   <ROOT>/homework_iot/fms-io   (이 스크립트는 fms-io/deploy/ 안)
#   <ROOT>/AMR_client
#
# 실행: bash homework_iot/fms-io/deploy/setup_vm.sh
#   로봇 시작 위치 지정: ROBOT_ARGS="--x 1.2 --y 6.0" bash .../setup_vm.sh
# 다시 실행해도 된다(서비스 파일을 덮어쓰고 재시작).
set -euo pipefail

FMS_IO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$FMS_IO/../.." && pwd)"
AMR_CLIENT="$ROOT/AMR_client"
RUN_USER="$(id -un)"
ROBOT_ID="${ROBOT_ID:-AMR-001}"
ROBOT_ARGS="${ROBOT_ARGS:-}"

[[ -f "$FMS_IO/fms_server/main.py" ]] || { echo "fms-io 를 찾지 못함: $FMS_IO"; exit 1; }
[[ -x "$AMR_CLIENT/robot_client" ]] || { echo "robot_client 가 없음. $AMR_CLIENT 에서 make clean && make 먼저"; exit 1; }
head -c 4 "$AMR_CLIENT/robot_client" | grep -q ELF || { echo "robot_client 가 리눅스 바이너리가 아님(Mac 빌드?). make clean && make"; exit 1; }

echo "== venv + 패키지"
cd "$FMS_IO"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt -r server/requirements.txt

echo "== 브로커(mosquitto 시스템 서비스)"
sudo systemctl enable --now mosquitto

echo "== systemd 서비스 등록"
write_unit() {  # write_unit 이름 내용
    echo "$2" | sudo tee "/etc/systemd/system/$1.service" >/dev/null
}

write_unit iot-fms "[Unit]
Description=IoT demo - FMS server (:8000)
After=network-online.target mosquitto.service
Wants=mosquitto.service

[Service]
User=$RUN_USER
WorkingDirectory=$FMS_IO
Environment=PYTHONUNBUFFERED=1
ExecStart=$FMS_IO/.venv/bin/uvicorn fms_server.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target"

write_unit iot-pi "[Unit]
Description=IoT demo - Raspberry Pi simulator (:5001)
After=network-online.target iot-fms.service

[Service]
User=$RUN_USER
WorkingDirectory=$FMS_IO
Environment=PYTHONUNBUFFERED=1
ExecStart=$FMS_IO/.venv/bin/python server/io_server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target"

# --map 없이 띄우면 FMS 가 등록된 맵을 SET_MAP 으로 내려준다(같은 VM 이라 public_url 기본값 localhost 로 충분)
write_unit iot-robot "[Unit]
Description=IoT demo - robot client $ROBOT_ID
After=network-online.target mosquitto.service iot-fms.service
Wants=mosquitto.service

[Service]
User=$RUN_USER
WorkingDirectory=$AMR_CLIENT
ExecStart=$AMR_CLIENT/robot_client --id $ROBOT_ID --host localhost $ROBOT_ARGS
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target"

sudo systemctl daemon-reload
sudo systemctl enable iot-fms iot-pi iot-robot
sudo systemctl restart iot-fms iot-pi iot-robot

echo "== 확인"
sleep 3
systemctl --no-pager --lines=0 status iot-fms iot-pi iot-robot mosquitto | grep -E "●|Active:"
curl -s -o /dev/null -w "FMS  :8000 -> HTTP %{http_code}\n" http://127.0.0.1:8000/ui/fms.html || true
curl -s -o /dev/null -w "Pi   :5001 -> HTTP %{http_code}\n" http://127.0.0.1:5001/ || true

IP="$(curl -s -m 3 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)"
echo
echo "접속: http://${IP:-<외부IP>}:8000/ui/fms.html   http://${IP:-<외부IP>}:5001/"
echo "로그: journalctl -u iot-fms -f   (iot-pi, iot-robot 도 같은 방식)"
echo "중지: sudo systemctl stop iot-robot iot-pi iot-fms"
