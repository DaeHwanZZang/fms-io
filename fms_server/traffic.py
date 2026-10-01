"""트래픽 제어 — 시공간 예약, 데드락 처리 (프로젝트 핵심).

Order.OrderNode.released 로 통행권을 부여/보류한다. 정지 명령이 아니라
"안전이 확보된 노드만 released=true" 방식 (CLAUDE.md 핵심 메커니즘 참고).
"""
