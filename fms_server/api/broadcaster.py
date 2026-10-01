"""
Fleet 변경 -> WebSocket 브로드캐스트 다리
=======================================

`Fleet` 리스너는 paho-mqtt 네트워크 스레드에서 호출된다. WebSocket 전송은
asyncio 이벤트 루프에서 해야 한다. 이 클래스가 둘을 잇는다:

    paho 스레드:  listener -> loop.call_soon_threadsafe(queue.put_nowait, event)
    asyncio:      pump 태스크가 queue 에서 꺼내 모든 소켓에 보낸다

lifespan 에서 `attach(fleet)` 로 리스너를 걸고 `start()` 로 pump 를 띄운다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from starlette.websockets import WebSocket, WebSocketState

from fms_server.fleet import Fleet, FleetEvent
from fms_server.api.views import RobotView, build_robot_view
from fms_server.sites import SiteRegistry

log = logging.getLogger("fms.ws")


class FleetBroadcaster:
    def __init__(self, fleet: Fleet, registry: SiteRegistry) -> None:
        self._fleet = fleet
        self._registry = registry
        self._clients: set[WebSocket] = set()
        self._queue: asyncio.Queue[FleetEvent] = asyncio.Queue(maxsize=1000)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._unsubscribe = None

    # -- 수명 주기 (lifespan 에서) --------------------------------------

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsubscribe = self._fleet.add_listener(self._on_fleet_event)
        self._pump_task = self._loop.create_task(self._pump())
        log.info("WebSocket 브로드캐스터 시작")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        for ws in list(self._clients):
            await self._safe_close(ws)
        self._clients.clear()
        log.info("WebSocket 브로드캐스터 중지")

    # -- 소켓 등록 -------------------------------------------------------

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        # 접속 즉시 현재 스냅샷을 보낸다.
        await self._send(ws, {
            "type": "snapshot",
            "robots": [self._view(r).model_dump() for r in self._fleet.all()],
        })

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def _view(self, record) -> RobotView:
        return build_robot_view(record, self._fleet, self._registry)

    # -- 내부 ----------------------------------------------------------

    def _on_fleet_event(self, event: FleetEvent) -> None:
        """paho 스레드에서 호출됨. 이벤트 루프로 넘기기만 한다."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            pass  # 루프가 이미 닫힘 (종료 중)

    def _enqueue(self, event: FleetEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("브로드캐스트 큐 가득참 — 이벤트 드롭 (%s %s)", event.kind, event.robot_id)

    async def _pump(self) -> None:
        while True:
            event = await self._queue.get()
            if not self._clients:
                continue
            message = {
                "type": "event",
                "kind": event.kind,
                "robot_id": event.robot_id,
                "robot": (
                    self._view(event.record).model_dump()
                    if event.kind != "removed"
                    else None
                ),
            }
            for ws in list(self._clients):
                await self._send(ws, message)

    async def _send(self, ws: WebSocket, message: dict) -> None:
        if ws.application_state != WebSocketState.CONNECTED:
            self._clients.discard(ws)
            return
        try:
            await ws.send_json(message)
        except Exception:
            self._clients.discard(ws)
            await self._safe_close(ws)

    async def _safe_close(self, ws: WebSocket) -> None:
        try:
            await ws.close()
        except Exception:
            pass
