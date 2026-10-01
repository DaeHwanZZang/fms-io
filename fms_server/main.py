"""FMS 서버 진입점 (FastAPI).

    .venv/bin/uvicorn fms_server.main:app --reload

수명 주기:
    시작: SiteRegistry(디스크에서 site 로드) -> Fleet -> MQTT 구독
          -> SiteCoordinator (등록 로봇을 배정 맵과 SET_MAP 동기화)
          -> WebSocket 브로드캐스터
    종료: 역순

브로커(`MQTT_broker`)가 안 떠 있어도 서버는 뜬다. paho 가 재연결을 계속 시도한다
(`FMS_MQTT_REQUIRED=1` 이면 시작 시 연결 실패를 예외로 올린다).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fms_server.api.broadcaster import FleetBroadcaster
from fms_server.api.flags import router as flags_router
from fms_server.api.jobs import router as jobs_router
from fms_server.api.locations import router as locations_router
from fms_server.api.robots import router as robots_router
from fms_server.api.sites import router as sites_router
from fms_server.config import settings
from fms_server.coordinator import SiteCoordinator
from fms_server.fleet import Fleet
from fms_server.mqtt_client import FmsMqttClient
from fms_server.sites import SiteRegistry
from fms_server.flags import FlagStore
from fms_server.io_client import PiIoClient
from fms_server.task import JobCoordinator, JobStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("fms.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = SiteRegistry(settings.data_root)
    fleet = Fleet(stale_after=settings.robot_stale_after)
    mqtt = FmsMqttClient(fleet, settings)
    coordinator = SiteCoordinator(fleet, registry, mqtt, settings.public_url)
    broadcaster = FleetBroadcaster(fleet, registry)
    jobs = JobStore(settings.data_root)
    flags = FlagStore()
    job_coordinator = JobCoordinator(fleet, registry, jobs, mqtt, flags, PiIoClient(settings.pi_url))

    app.state.settings = settings
    app.state.sites = registry
    app.state.fleet = fleet
    app.state.mqtt = mqtt
    app.state.coordinator = coordinator
    app.state.broadcaster = broadcaster
    app.state.jobs = jobs
    app.state.flags = flags
    app.state.job_coordinator = job_coordinator

    broadcaster.start()
    coordinator.start()
    job_coordinator.start()
    mqtt.start()
    log.info("FMS 서버 시작 완료")
    try:
        yield
    finally:
        mqtt.stop()
        job_coordinator.stop()
        coordinator.stop()
        await broadcaster.stop()
        log.info("FMS 서버 종료")


app = FastAPI(title="FMS Server", version="0.1.0", lifespan=lifespan)
app.include_router(robots_router)
app.include_router(sites_router)
app.include_router(locations_router)
app.include_router(jobs_router)
app.include_router(flags_router)

# [homework_iot 추가] 관제 웹 UI(fms-io/web)를 같은 오리진 /ui 로 서빙한다. 원본 AMR_master 에는 없음.
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_WEB_DIR, html=True), name="ui")

_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"


@app.get("/", include_in_schema=False)
def admin_page() -> FileResponse:
    """조작용 최소 관리 UI (기존 REST API 를 fetch 로 호출하는 정적 HTML 한 장)."""
    return FileResponse(_ADMIN_HTML)


@app.get("/health")
def health(request: Request) -> dict:
    fleet = request.app.state.fleet
    mqtt = request.app.state.mqtt
    registry = request.app.state.sites
    return {
        "status": "ok",
        "mqtt_connected": mqtt.connected,
        "robots_known": len(fleet.all()),
        "sites": len(registry.list_sites()),
        "jobs": len(request.app.state.jobs.list()),
    }
