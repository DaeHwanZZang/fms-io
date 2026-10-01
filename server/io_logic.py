"""
소켓/GPIO 코드가 없는 순수 로직. io_server.py 가 이 결과를 핀과 브로드캐스트에 반영한다.
"""

from __future__ import annotations

def to_level(logical: bool, active_low: bool) -> int:
    """논리값(1=활성) -> 전기 레벨."""
    return int((not logical) if active_low else bool(logical))


def to_logical(level: int, active_low: bool) -> bool:
    return (not level) if active_low else bool(level)


def flag_values(inputs: dict[str, bool], outputs: dict[str, bool]) -> dict[str, bool]:
    """
    FMS 로 올릴 신호. inputs/outputs 는 장치 이름 -> 논리값(1=활성).
    원시 상태(모든 장치 이름 그대로)가 기본이고, 조건은 FMS job 의 wait_flag 단계에서 정한다.
    """
    return {**inputs, **outputs}
