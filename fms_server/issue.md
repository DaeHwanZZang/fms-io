# Known issues

## 해결됨

- ~~job 실행 중 pause/resume 하면 job 자체를 처음부터 다시 시작함~~
  → `AMR_client` `OrderExecutor` 수정 (2026-09-10). 원인: `consume_reached_nodes`
  가 A\* 경유점이 노드당 여러 개면 세그먼트 중간에서 `current_index_` 를 전진시키지
  못해, EMERGENCY/PAUSE/MANUAL 해제 후 `replan` 이 세그먼트 첫 노드부터 다시 계획.
  `segment_wp_bounds_` (노드별 누적 경유점 수) 로 지나온 노드를 추적하도록 변경.
  이제 재개 시 "다음 미도달 노드" 부터 이어서 진행. FATAL/ERROR 는 기존대로 오더
  버리고 FMS 가 job 을 failed 처리.
