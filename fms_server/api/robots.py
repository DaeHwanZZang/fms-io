"""로봇 레지스트리 REST + WebSocket 라우터.

    GET  /api/v1/robots            전체 목록 + 요약
    GET  /api/v1/robots/{id}       한 대 상세
    WS   /api/v1/robots/stream     스냅샷 1회 + 이후 변경 이벤트 스트림

발행 계열(order/instant)은 allocator/traffic 이 붙을 때 추가한다. 골격 단계에서는
읽기 전용.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from fms_server.api.views import FleetView, RobotView, build_robot_view

log = logging.getLogger("fms.api.robots")

router = APIRouter(prefix="/api/v1", tags=["robots"])


@router.get("/robots", response_model=FleetView)
def list_robots(request: Request) -> FleetView:
    return FleetView.from_fleet(request.app.state.fleet, request.app.state.sites)


@router.get("/robots/{robot_id}", response_model=RobotView)
def get_robot(robot_id: str, request: Request):
    fleet = request.app.state.fleet
    record = fleet.get(robot_id)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": f"로봇 {robot_id} 를 모른다"})
    return build_robot_view(record, fleet, request.app.state.sites)


@router.websocket("/robots/stream")
async def robots_stream(ws: WebSocket) -> None:
    broadcaster = ws.app.state.broadcaster
    await broadcaster.register(ws)
    try:
        while True:
            # 클라이언트가 보내는 건 무시한다 (순수 관측 스트림). 연결 유지용.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unregister(ws)
