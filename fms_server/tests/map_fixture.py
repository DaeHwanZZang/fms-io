"""외부 AMR_client 저장소 대신 배포에 포함한 벤더 맵을 사용한다."""
from pathlib import Path

VENDOR = (
    Path(__file__).resolve().parents[1]
    / "maps/sites/sited_test0920/maps/test0920/9799b98bcb6c/source"
)
