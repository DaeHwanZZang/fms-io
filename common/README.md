# common — 프로토콜 원본 (source of truth)

`schemas.py`(MQTT 메시지 계약: Order/State/Connection/InstantAction),
`map_model.py`(맵 PNG+JSON, 좌표 변환)의 **원본**. `fms_server`는 여기서 바로
import 한다 (같은 레포).

`AMR_client`(C++ 포팅), `AMR_viewer`(파일 복사)는 이걸 베껴간 사본이다 —
여기가 바뀌면 그쪽도 반드시 수동으로 맞출 것. 상세 규칙은 상위
[`../CLAUDE.md`](../CLAUDE.md)의 "동기화 규칙" 참고.
