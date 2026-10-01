# fms_server — FMS 백엔드 (FastAPI)

로봇에게 order 를 내려보내고 state 를 수집하는 중앙 서버. 아직 골격 단계.

## 구조

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 진입점 (lifespan: SiteRegistry→Fleet→MQTT→Coordinator→Broadcaster) |
| `config.py` | 설정 (환경변수 `FMS_MQTT_HOST`, `FMS_DATA_ROOT`, `FMS_PUBLIC_URL` 등) |
| `sites.py` | `SiteRegistry` — site 등록, 로봇 등록+맵 배정, site.json 영속화 |
| `locations.py` | `LocationStore` — 관리자가 `(x,y,theta)` 에 이름 붙임 (site 스코프), `locations.json` 영속화 |
| `task.py` | `JobStore` + `JobCoordinator` — job(명령 묶음, 지금은 move) 생성/디스패치/취소, 로봇 state 로 상태 되짚기 |
| `coordinator.py` | 등록 로봇을 배정 맵 활성 버전과 `SET_MAP` 으로 동기화 |
| `fleet.py` | 로봇 상태 레지스트리 (state/connection 최신값 + liveness 판정) |
| `mqtt_client.py` | 로봇과 MQTT 송수신. `fms/v1/+/state,connection` 구독 → `fleet` 반영 |
| `mapkit/ingest.py` | 벤더 `grid_cfg.grid` → `fms_map.json` 생성/검증 (CLI 도 됨) |
| `mapkit/bake.py` | despeckle + prohibit zone 굽기 + 정규화된 `MapAnnotations` 생성 |
| `mapkit/store.py` | 맵 저장소 (site 별로 인스턴스화) — 업로드/버전/활성/번들 |
| `api/sites.py` | Site/Map/Robot REST (`/api/v1/sites/...`) |
| `api/locations.py` | Named location REST (`/api/v1/sites/{id}/locations`) |
| `api/jobs.py` | job REST (`/api/v1/sites/{id}/jobs` + `/dispatch`, `/cancel`) |
| `static/admin.html` | 조작용 최소 관리 UI (`GET /`) — 정적 HTML 한 장, REST 를 fetch |
| `api/robots.py` | `GET /api/v1/robots`, `/robots/{id}`, `WS /robots/stream` |
| `api/broadcaster.py` | paho 스레드 → asyncio WebSocket 브로드캐스트 다리 |
| `api/views.py` | REST/WS 응답 DTO (등록 정보로 보강) |
| `tests/` | `pytest fms_server/tests/` |
| `allocator.py` | 작업 할당 (아직 골격) |
| `traffic.py` | 시공간 예약, 데드락 처리 — 프로젝트 핵심 (아직 골격) |
| `planner.py` | 교차로 예약용 경로계획 (필요해지면. 로봇 개별 회피는 `AMR_client` 몫) |
| `battery.py` | 충전 스케줄링 (후순위) |

## 원칙

- 로봇을 직접 시뮬레이션하지 않는다 — 아는 건 로봇이 MQTT로 보고한 값뿐
  (`../common/schemas.py` 의 `State`)
- `AMR_client`/`AMR_viewer` 의 코드를 import 하지 않는다 (별도 레포라 물리적으로도 불가능)
- 메시지 계약은 `../common/schemas.py` 가 원본 — 여기서 바로 import 해서 쓴다
  (같은 레포 안이라 사본 동기화 불필요, `AMR_client`/`AMR_viewer` 만 수동 동기화 대상)

## 맵 좌표계 — `fms_map.json` 은 손으로 쓰지 않는다

SLAM 벤더 맵 폴더의 좌표계 원본은 **`grid_cfg.grid`** 다. `fms_map.json` 은
거기서 생성한다.

```bash
# 레포 루트(AMR_master/)에서 실행한다

# 검증만
.venv/bin/python -m fms_server.mapkit.ingest AMR_client/maps/<맵ID>

# fms_map.json 생성/덮어쓰기
.venv/bin/python -m fms_server.mapkit.ingest AMR_client/maps/<맵ID> --write
```

손으로 쓰면 무슨 일이 나는지: `641931de9eae7cecb34d5765` 의 `fms_map.json` 은
origin 이 정확히 2배(`-42.253462` / 벤더 `ox: -21.128462`)로 적혀 있었다.
그 좌표계에서는 `zone_meta.json` 의 금지구역 7개 중 6개가 맵 밖으로 떨어지고,
남은 것도 엉뚱한 픽셀을 가리킨다. 고친 뒤에는 금지구역 6개가 벤더가 실제로
검게 칠해 둔 영역과 85~100% 겹친다 — 좌표계가 맞다는 독립적인 증거다.

## Site / Map / Robot (`FMS_DATA_ROOT`, 기본 `fms_server/maps/`)

규약은 `../CLAUDE.md` "맵 관리 규약" 참고. 디스크 레이아웃:

```
sites/{site_id}/
  site.json                     {name, created_at, robots:{id:{map_id, registered_at}}}
  locations.json                {locations:{<name>:{map_id, x, y, theta, type, ...}}}
  jobs.json                     {seq, jobs:{<job_id>:{commands, status, robot_id, ...}}}
  maps/{map_id}/
    {version}/
      fms_map.json              image -> navi_gridmap.baked.png
      navi_gridmap.baked.png    prohibit zone 구운 grid (로봇이 쓰는 것)
      annotations.json          정규화된 location/zone (MapAnnotations)
      manifest.json             {version, files:{name:sha256}, stats, warnings}
      source/                   벤더 원본 (재굽기용, 번들엔 미포함)
    active                      활성 버전 한 줄
```

- **`map_id`** ("floor-1") = 관리자 라벨, 저장 폴더명, REST 경로. site 안에서만 유일.
  **`map_uid`** = `sha256(site_id/map_id)[:24]` = FMS 전역 id. 로봇이 보는 맵 이름
  (`fms_map.json` name, `annotations.json` map_id, SET_MAP params, `State.map_id`).
  결정적이라 별도 상태파일 없음. 벤더 폴더명(남의 FMS ObjectId)은 버려진다.
- **버전** = `fms_map.json` + `baked.png` + `annotations.json` 의 sha256 결합 재해시
  앞 12자. 같은 입력 + 같은 despeckle → 같은 버전 (멱등 업로드).
  `map_uid` 는 버전 무관 (맵 정체성 = 물리 구역, version = 개정).
- **prohibit zone 은 서버에서 grid 에 굽는다** → 로봇 A* 코드 무수정으로 회피.
- **despeckle 기본 off** — 현재 구현은 OCCUPIED→UNKNOWN 재분류라 주행성 영향 없음.
- 맵 전송은 **HTTP** (`GET .../bundle`). MQTT 는 `SET_MAP` 알림만.
  코디네이터가 로봇 등록/재배정 시 + 맵 activate 시 배정 로봇에 발행.
  `url` 조립에 `FMS_PUBLIC_URL` 사용 — 로봇과 FMS 가 다른 PC 면 FMS PC 의 IP.
- **미등록 로봇** — state 는 수집(대시보드에 `UNREGISTERED` 로 보임), job·SET_MAP
  대상 아님. 관리자가 `POST /sites/{id}/robots` 해야 편입.

업로드 필수: `grid_cfg.grid`, 항법 PNG, `location_meta.json`, `zone_meta.json`
(+ `map_meta.json` 선택). `fms_map.json` 은 FMS 가 재생성. `*.surfel`/`*.clmap`/
중복 PNG/`*.old` 는 빼라 (`source/` 에 쌓이기만 함).

```bash
# site 등록 → 맵 업로드(구역) → 로봇 등록(맵 배정)
curl -X POST localhost:8000/api/v1/sites -d '{"site_id":"hq","name":"본사"}'
curl -X POST localhost:8000/api/v1/sites/hq/maps -F map_id=floor-1 -F activate=true \
  $(for f in maps/<벤더폴더>/*; do echo -F files=@$f; done)
curl -X POST localhost:8000/api/v1/sites/hq/robots -d '{"robot_id":"AMR-001","map_id":"floor-1"}'

# 이름 붙인 좌표 (job 이 나중에 raw 좌표 대신 이 이름으로 목적지 지정)
curl -X POST localhost:8000/api/v1/sites/hq/locations \
  -d '{"name":"충전소-A","map_id":"floor-1","x":1.2,"y":3.4,"theta":0.0,"type":"charger"}'
```

### Named location

관리자가 `(x, y, theta)` 에 이름을 붙인다. **site 스코프** — 이름은 site 전체에서
유일, location 마다 어느 맵 프레임인지(`map_id`)를 함께 갖는다. `theta` 는 선택
(기본 0.0, `normalize_theta` 로 -pi~pi 접힘). `type` 은 `LocationType` 값 (모르는
값은 `other`). 맵 밖/주행불가 셀 좌표는 거부하지 않고 `warnings` 로만 알린다.
벤더 맵 `annotations.json` 의 location 과는 별개 (그쪽은 버전별 불변).

```
GET/POST       /api/v1/sites/{site_id}/locations
GET/PUT/DELETE /api/v1/sites/{site_id}/locations/{name}
```

### job (작업)

job = 로봇이 수행할 명령의 순서 묶음. **FMS 안에만 있다** — 로봇은 job 을 모르고
`Order` 만 받는다. `JobCoordinator` 가 명령을 `Order` 로 컴파일해 발행하고, 로봇이
보고한 `State` 로 job 상태를 되짚는다. 규약은 `task.py` 참고.

- 명령: **지금은 `move` 만** (target = named location 이름). docking/charging/wait 미구현
- 상태: `pending` → `working`(디스패치) → `success`/`failed`(로봇 보고) / `canceled`(취소)
- 디스패치 게이팅: 로봇이 `available` + 배정 맵 활성 버전과 `State.map_version` 일치
- 한 job 의 목적지는 전부 같은 맵, 그 맵 = 로봇 배정 맵이어야 함
- 지금은 모든 노드 `released=true` (트래픽 제어 붙기 전)
- `success` 판정: 로봇이 우리 `order_id` 를 한 번 보고한 뒤 그걸 비우고 FATAL 없음

```
GET/POST                    /api/v1/sites/{site_id}/jobs
GET/DELETE                  /api/v1/sites/{site_id}/jobs/{job_id}
POST .../jobs/{job_id}/dispatch   {robot_id?}   pending -> working, Order 발행
POST .../jobs/{job_id}/cancel                   working 이면 CANCEL_ORDER instant
```

```bash
curl -X POST localhost:8000/api/v1/sites/hq/jobs \
  -d '{"robot_id":"AMR-001","commands":[{"type":"move","target":"충전소-A"}]}'
curl -X POST localhost:8000/api/v1/sites/hq/jobs/hq-J0001/dispatch -d '{}'
```

## 실행

```bash
# 레포 루트(AMR_master/)에서
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn fms_server.main:app --reload
# 브라우저로 http://localhost:8000/  → 조작용 관리 UI (site/맵/로봇 등록, 서버 상태, 라이브 로봇)

# 테스트 (브로커 불필요 — MQTT 는 connect_async 라 안 붙어도 뜬다)
.venv/bin/python -m pytest fms_server/tests/ -q
```

브로커(`MQTT_broker`)가 없어도 서버는 뜨고, paho 가 재연결을 계속 시도한다.
`FMS_MQTT_REQUIRED=1` 이면 시작 시 연결 실패를 예외로 올린다.

환경변수: `FMS_MQTT_HOST`(localhost), `FMS_MQTT_PORT`(1883),
`FMS_MQTT_KEEPALIVE`(30), `FMS_ROBOT_STALE_AFTER`(3.0 — state 가 이 초 넘게
안 오면 STALE), `FMS_MQTT_REQUIRED`(0), `FMS_DATA_ROOT`(fms_server/maps —
site/맵 영속 데이터), `FMS_PUBLIC_URL`(http://localhost:8000 — SET_MAP url 조립).

상세는 `../CLAUDE.md` 참고.
