# FMS 사본 출처

`fms_server/`, `common/`, `requirements.txt`는 `../AMR_master`(커밋 c0e383c, 2026-09-11)에서 복사한 것이다.
원본은 수정하지 않는다. 자동 동기화는 없다. 원본이 바뀌면 다시 복사한다.

- 실행은 이 디렉터리(`fms-io/`)에서 한다. `common.schemas`, `fms_server.*` 임포트와 기본 `FMS_DATA_ROOT=fms_server/maps` 상대경로가 여기를 기준으로 한다.
- `fms_server/maps/`는 원본에서 gitignore된 런타임 데이터(등록된 site/맵)를 그대로 복사한 스냅샷이다.

## 원본과 다른 점

- `fms_server/main.py`: `web/`을 `/ui`로 마운트하는 5줄 추가(`[homework_iot 추가]` 주석). 그 외 원본과 동일.
- `fms_server/locations.py`, `api/locations.py`, `task.py`: location `keep_theta` 옵션 추가(`[homework_iot 추가]`). 켜진 location 이 job 중간 경유지면 `compile_to_order` 가 노드에 짧은 WAIT 액션을 붙여 로봇이 멈춰 theta 로 정렬하게 한다. `tests/test_task.py` 에 테스트 2개 추가.
- `fms_server/task.py`: `JobCoordinator` 큐 자동 배정 추가(`[homework_iot 추가]`). 로봇이 working 이어도 job 은 pending 으로 쌓이고, 로봇이 한가해지면(state 이벤트마다) 먼저 만든 순서로 자기 지정 job 또는 로봇 미지정(`robot_id` 없음) job 을 디스패치한다. `tests/test_task.py` 에 테스트 3개 추가.
- `fms_server/task.py`, `flags.py`, `api/flags.py`, `api/jobs.py`, `main.py`: job 명령 `wait`(초) / `wait_flag`(외부 플래그가 내려갈 때까지) 추가(`[homework_iot 추가]`). wait 는 FMS 쪽 명령이라 job 이 wait 에서 쪼개져 move 구간마다 Order 가 나간다(order_id `{job_id}~c{시작 명령 번호}`). 플래그는 `PUT /api/v1/flags/{name}` 로 라즈베리파이가 올리고 내린다(메모리 저장, 미수신 플래그는 계속 대기). `tests/test_task.py` 에 테스트 5개 추가. `wait_flag` 는 `until`(lowered 기본 | raised), `hold`, `timeout`/`on_timeout`, 신호 만료(`FLAG_STALE_SEC`)를 지원한다(테스트 4개 추가).
- `fms_server/task.py`, `io_client.py`, `config.py`(`FMS_PI_URL`, 기본 `http://127.0.0.1:5001`), `main.py`, `api/jobs.py`: job 명령 `set_output`(device, value 0/1) 추가(`[homework_iot 추가]`). 해당 단계에서 FMS 가 라즈베리파이 `POST /api/output` 을 호출한다(별도 스레드, 실패하면 job failed). 테스트 4개 추가.
