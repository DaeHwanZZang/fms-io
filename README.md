# FMS–현장 설비 I/O 연동

AMR 관제 서버(FMS)와 라즈베리파이 설비를 연결하는 IoT 수업 프로젝트입니다. 웹에서 지도·로봇·작업을 확인하고, 경광등·부저·푸시 버튼 상태를 주고받습니다. Windows와 macOS에서는 GPIO를 가상 장치로 대체하므로 라즈베리파이 없이 실행할 수 있습니다.

> **처음 실행하는 분께:** Python 설치 → 저장소 다운로드 및 압축 해제 → 실행 파일 열기 순서로 진행하세요. 예제 지도는 포함되어 있습니다. 로봇이 보이지 않는 것은 로봇 클라이언트가 연결되지 않았기 때문입니다.

## 준비물

- Python **3.11 이상**. 새로 설치한다면 3.12 또는 3.13을 권장합니다. macOS 실제 검증 환경은 Python 3.14.7입니다.
- 최신 웹 브라우저
- 인터넷: 첫 패키지 설치 및 웹 화면의 Socket.IO 라이브러리(CDN) 로딩에 필요합니다.
- Git은 선택 사항입니다. GitHub의 ZIP 다운로드로도 실행할 수 있습니다.

Python은 [공식 다운로드 페이지](https://www.python.org/downloads/)에서 운영체제에 맞게 설치합니다. Windows에서는 설치 화면의 **Add python.exe to PATH**를 선택하세요. 설치 후 터미널을 새로 여세요.

## GitHub에서 내려받기

저장소: [DaeHwanZZang/fms-io](https://github.com/DaeHwanZZang/fms-io)

[ZIP 파일 다운로드](https://github.com/DaeHwanZZang/fms-io/archive/refs/heads/main.zip)로 바로 내려받을 수도 있습니다.

**ZIP 방식:** GitHub 저장소에서 **Code → Download ZIP**을 누르고 압축을 풉니다. 압축 파일 안에서 실행하지 마세요.

**Git 방식:** 원하는 상위 폴더에서 실행합니다.

```sh
git clone https://github.com/DaeHwanZZang/fms-io.git fms-io
cd fms-io
```

아래 안내에서 ‘프로젝트 루트’는 `run.py`, `start.bat`, `start.command`가 있는 폴더입니다. GitHub에 이 프로젝트가 하위 폴더로 올라간 경우 그 폴더까지 들어가세요.

## Windows 실행

1. 프로젝트 루트의 **`start.bat`을 더블클릭**합니다.
2. 첫 실행에는 패키지 설치가 끝날 때까지 기다립니다.
3. 브라우저에 관제 화면과 설비 I/O 화면이 열립니다.
4. 실행한 터미널 창은 계속 열어 둡니다. 종료하려면 그 창에서 **Ctrl+C**를 누릅니다. 일괄 작업 종료 질문이 나오면 `Y`를 입력합니다.

명령으로 실행하려면 **PowerShell 또는 명령 프롬프트, 프로젝트 루트**에서:

```powershell
py -3 run.py
```

`py`가 없고 `python` 명령이 설치되어 있다면 `python run.py`를 사용하세요. PowerShell의 스크립트 실행 정책 변경이나 관리자 권한은 필요하지 않습니다.

## macOS 실행

프로젝트 루트의 **`start.command`를 더블클릭**합니다. 실행 권한 때문에 열리지 않거나 ZIP 다운로드로 권한이 보존되지 않았다면 **터미널, 프로젝트 루트**에서:

```sh
python3 run.py
```

첫 실행에는 패키지를 설치하고 두 서버를 시작합니다. 관제·설비 화면이 자동으로 열립니다. 종료는 실행한 터미널에서 **Ctrl+C**입니다.

두 운영체제 모두 가상환경 `.venv`를 자동 생성합니다. 패키지 목록이 바뀌면 다음 실행에 다시 설치합니다. 브라우저 자동 열기가 필요 없으면 `run.py` 실행 명령 뒤에 `--no-browser`를 붙이세요.

## 접속 주소와 화면 역할

| 주소 | 용도 |
|---|---|
| http://localhost:8000/ui/fms.html | 지도·로봇 상태·작업·설비 신호를 보는 FMS 관제 |
| http://localhost:5001/ | 가상 푸시 버튼, 경광등, 부저 조작 및 핀 상태 확인 |
| http://localhost:8000/ | 사이트·지도·로봇 등록용 관리 화면 |
| http://localhost:8000/docs | REST API 명세와 요청 실행 |
| http://localhost:8000/health | FMS 동작 및 MQTT 연결 상태 |

관제 화면은 **Monitor / Jobs / Signals** 탭으로 구성됩니다. `Site setup`은 관리 화면을 엽니다. `Move`는 이동, `Wait`는 시간 대기, `Signal`은 입력 조건 대기, `Output`은 경광등·부저 출력 단계입니다.

## 하드웨어 없이 먼저 확인하기

1. 두 서버를 실행하고 FMS 화면에서 예제 사이트 `test0920`과 지도를 확인합니다.
2. 설비 화면에서 푸시 버튼을 누르거나 놓습니다.
3. FMS의 **Signals** 탭에서 `button`의 값이 `raised`/`lowered`로 바뀌는지 확인합니다. `raised`는 눌림/켜짐, `lowered`는 해제/꺼짐입니다.
4. 설비 화면에서 경광등과 부저 출력을 수동으로 켜고 끕니다. 경광등은 한 번에 한 색만 켜집니다.
5. 이벤트 기록과 FMS 신호 변화를 확인합니다. 신호는 변경 즉시 전송하고 약 2초마다 다시 전송합니다.

이 과정에는 MQTT와 로봇이 필요하지 않습니다. 예제에는 등록 정보와 과거 작업 기록이 들어 있으므로 기록이 보인다는 사실만으로 현재 로봇이 연결되었다고 판단하면 안 됩니다. 실제 로봇 상태는 별도로 수신해야 합니다.

**로봇 이동과 작업 실행은 별도 준비가 필요합니다.** MQTT 브로커와 호환 AMR 클라이언트는 이 저장소에 포함되지 않습니다. 가짜 로봇 상태를 만들어 표시하지 않습니다. I/O 단계만 있는 job도 현 구현에서는 로봇 배정·연결 조건을 충족해야 실행됩니다. 장비 없는 확인은 위 수동 I/O 절차를 사용하세요.

## 로봇·라즈베리파이 연결

로봇까지 연결하려면 MQTT 브로커를 실행하고 호환 AMR 클라이언트를 연결한 뒤, 관리 화면에서 로봇에 활성 지도를 배정합니다. 로봇이 보고한 지도 버전과 배정된 활성 버전이 일치하고 로봇이 사용 가능한 상태여야 작업을 보낼 수 있습니다.

| 환경변수 | 기본값 | 변경할 때 |
|---|---|---|
| `FMS_MQTT_HOST` | `localhost` | MQTT 브로커가 다른 PC에 있을 때 |
| `FMS_MQTT_PORT` | `1883` | 브로커 포트를 바꿀 때 |
| `FMS_PUBLIC_URL` | `http://localhost:8000` | 로봇이 내려받을 지도 URL을 지정할 때 |
| `FMS_PI_URL` | `http://127.0.0.1:5001` | 출력 명령을 보낼 실제 Pi 주소 |
| `FMS_URL` | `http://127.0.0.1:8000` | Pi에서 신호를 올릴 FMS 주소 |
| `FMS_DATA_ROOT` | `fms_server/maps` | 사이트·지도·작업 저장 경로 |
| `FMS_MQTT_REQUIRED` | `0` | MQTT 연결이 필수인 환경 |

환경변수는 실행 전에 셸에서 설정합니다. `.env` 자동 로드는 구현되어 있지 않습니다. macOS(zsh, 프로젝트 루트) 예: `export FMS_MQTT_HOST=192.168.0.10`. Windows PowerShell(프로젝트 루트) 예: `$env:FMS_MQTT_HOST="192.168.0.10"`.

기본 실행기는 FMS를 로컬 접속용으로 시작합니다. 다른 PC의 로봇/Pi를 연결할 때는 아래 수동 실행으로 FMS를 `--host 0.0.0.0`으로 실행하고, `FMS_PUBLIC_URL`에 FMS PC의 LAN 주소를 지정하세요. I/O 서버는 현재 `0.0.0.0:5001`에서 수신합니다. 인증·HTTPS가 없는 수업용 개발 서버이므로 공용 인터넷 배포용 구성은 아닙니다.

실물 Pi에서는 `server/requirements.txt` 외에 `rpi-lgpio`를 설치해야 합니다. GPIO 구성은 `server/io_config.py`에 모여 있습니다. BCM 기준 출력은 적색 17, 황색 27, 녹색 22, 부저 23이고 입력은 버튼 5입니다. PC에서는 이 패키지를 설치하지 마세요. 실물 배선과 전기적 동작은 이번 점검에서 검증하지 않았습니다.

## 서버를 각각 실행하기

자동 실행 대신 서버별 로그를 보고 싶을 때 사용합니다. **모든 명령은 프로젝트 루트에서 실행**합니다. 먼저 가상환경을 만들고 의존성을 설치합니다.

macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r server/requirements.txt
```

별도 터미널 두 개에서 각각 실행:

```sh
.venv/bin/python -m uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

```sh
.venv/bin/python server/io_server.py
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r server/requirements.txt
```

별도 터미널 두 개에서 각각 실행:

```powershell
.\.venv\Scripts\python.exe -m uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

```powershell
.\.venv\Scripts\python.exe server/io_server.py
```

## 테스트

가상환경과 패키지를 준비한 뒤 **프로젝트 루트**에서 실행합니다. 별도 브로커·Pi·외부 맵 저장소는 필요하지 않습니다. 테스트는 동봉된 예제 맵을 사용합니다. MQTT 연결 상태를 단정하는 테스트가 있으므로 테스트할 때 로컬 MQTT 브로커는 종료하세요.

macOS:

```sh
.venv/bin/python -m pytest fms_server/tests server -q
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest fms_server/tests server -q
```

검증 결과와 미확인 사항은 [제출 전 점검 보고서](docs/제출전_점검보고서.md)에 기록합니다.

## 문제가 생겼을 때

| 증상 | 확인 방법 |
|---|---|
| Python 명령을 찾지 못함 | Python 설치·PATH 설정 후 터미널을 새로 열기 |
| Python 버전 오류 | `python3 --version` 또는 `py -3 --version`으로 3.11 이상인지 확인 |
| 패키지 설치 실패 | 인터넷·프록시·설치 오류 로그 확인 후 같은 실행 파일로 재시도 |
| 8000 또는 5001 포트 사용 중 | 이전 실행 터미널에서 Ctrl+C 후 재실행. 실행기는 기존 프로그램을 강제 종료하지 않음 |
| 브라우저가 자동으로 열리지 않음 | 위 접속 주소를 직접 입력 |
| `I/O client unavailable` | CDN 라이브러리가 로딩되지 않음. 인터넷/브라우저 차단 설정 확인 |
| `I/O unavailable` 또는 `I/O offline` | I/O 서버가 실행 중인지, 5001 포트에 접속 가능한지 확인 |
| `mqtt_connected: false`, 로봇 목록 비어 있음 | MQTT/로봇 미연결. 지도·수동 I/O 확인은 가능 |
| 지도 화면을 파일로 열었더니 동작하지 않음 | HTML 더블클릭 대신 `http://localhost:8000/ui/fms.html`로 접속 |
| 작업이 대기 상태에 머무름 | 로봇 연결·지도 버전·로봇 가용 상태·신호 조건 확인 |

## 프로젝트 구조

```text
fms-io/
├── run.py                  # 설치 준비·서버 실행·종료 관리
├── start.bat               # Windows 진입점
├── start.command           # macOS 진입점
├── requirements.txt        # FMS·맵 처리·테스트 의존성
├── fms_server/             # FastAPI 서버, 작업 처리, MQTT, 예제 맵
├── common/                 # 통신 스키마와 지도 공용 모델
├── server/                 # Flask-SocketIO, GPIO, FMS 신호 업로드
├── web/                    # 빌드 도구 없는 HTML/JavaScript UI
├── tools/                  # 개발용 로봇 작업 요청 도구
├── deploy/                 # 기존 Linux VM 배포 도구 (PC 빠른 실행에는 불필요)
└── docs/                   # 제출 전 점검 보고서
```

FMS 기반 코드의 출처와 수정 내역은 [FMS_SOURCE.md](FMS_SOURCE.md)를 참고하세요. 지도·작업은 파일로 저장하고 설비 플래그는 메모리에 저장하므로 FMS 재시작 직후 신호가 다시 수신될 때까지 잠시 대기할 수 있습니다.
