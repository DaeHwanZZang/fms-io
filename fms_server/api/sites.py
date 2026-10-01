"""Site / Map / Robot 등록 REST 라우터.

Site (물리 시설) 아래에 맵(물리 구역별)과 로봇이 귀속된다. 맵 관리 규약은
`../CLAUDE.md` "맵 관리 규약" 참고.

    # Site
    POST   /api/v1/sites                                   {site_id, name}
    GET    /api/v1/sites
    GET    /api/v1/sites/{site_id}
    DELETE /api/v1/sites/{site_id}[?force=true]

    # Map (site 에 귀속)
    POST   /api/v1/sites/{site_id}/maps                    벤더 폴더 멀티파트 업로드
    GET    /api/v1/sites/{site_id}/maps
    GET    /api/v1/sites/{site_id}/maps/{map_id}
    GET    /api/v1/sites/{site_id}/maps/{map_id}/manifest
    GET    /api/v1/sites/{site_id}/maps/{map_id}/bundle    구운 번들 zip (로봇이 받음)
    GET    /api/v1/sites/{site_id}/maps/{map_id}/files/{f}
    POST   /api/v1/sites/{site_id}/maps/{map_id}/activate  {version, notify}

    # Robot (site 에 등록 + 맵 배정)
    POST   /api/v1/sites/{site_id}/robots                  {robot_id, map_id}
    GET    /api/v1/sites/{site_id}/robots
    PUT    /api/v1/sites/{site_id}/robots/{robot_id}       {map_id}  재배정
    DELETE /api/v1/sites/{site_id}/robots/{robot_id}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from fms_server.mapkit.store import MapStoreError
from fms_server.sites import SiteError, SiteNotFound

log = logging.getLogger("fms.api.sites")

router = APIRouter(prefix="/api/v1/sites", tags=["sites"])


# =============================================================================
# 응답 / 요청 모델
# =============================================================================

class CreateSiteRequest(BaseModel):
    site_id: str
    name: str = ""


class VersionInfoOut(BaseModel):
    version: str
    created_at: str
    is_active: bool


class MapSummaryOut(BaseModel):
    map_id: str
    map_uid: str
    active_version: str | None
    versions: list[VersionInfoOut]


class RobotAssignmentOut(BaseModel):
    robot_id: str
    site_id: str
    map_id: str
    registered_at: str


class SiteOut(BaseModel):
    site_id: str
    name: str
    created_at: str
    maps: list[MapSummaryOut]
    robots: list[RobotAssignmentOut]


class SiteBriefOut(BaseModel):
    site_id: str
    name: str
    created_at: str
    map_count: int
    robot_count: int


class IngestResultOut(BaseModel):
    site_id: str
    map_id: str
    map_uid: str
    version: str
    created: bool
    activated: bool
    notified_robots: list[str]
    warnings: list[str]
    stats: dict


class ActivateRequest(BaseModel):
    version: str
    notify: bool = True


class ActivateResultOut(BaseModel):
    site_id: str
    map_id: str
    version: str
    notified_robots: list[str]


class RegisterRobotRequest(BaseModel):
    robot_id: str
    map_id: str


class ReassignRobotRequest(BaseModel):
    map_id: str


class RegisterRobotResultOut(BaseModel):
    assignment: RobotAssignmentOut
    set_map_sent: bool


# =============================================================================
# 헬퍼
# =============================================================================

def _registry(request: Request):
    return request.app.state.sites


def _coordinator(request: Request):
    return request.app.state.coordinator


def _map_summary_out(summary) -> MapSummaryOut:
    return MapSummaryOut(
        map_id=summary.map_id,
        map_uid=summary.map_uid,
        active_version=summary.active_version,
        versions=[
            VersionInfoOut(version=v.version, created_at=v.created_at, is_active=v.is_active)
            for v in summary.versions
        ],
    )


def _assignment_out(a) -> RobotAssignmentOut:
    return RobotAssignmentOut(
        robot_id=a.robot_id, site_id=a.site_id, map_id=a.map_id, registered_at=a.registered_at
    )


def _404(msg: str) -> HTTPException:
    return HTTPException(status_code=404, detail=msg)


# =============================================================================
# Site
# =============================================================================

@router.post("", response_model=SiteBriefOut)
def create_site(body: CreateSiteRequest, request: Request) -> SiteBriefOut:
    try:
        site = _registry(request).create_site(body.site_id, body.name)
    except SiteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return SiteBriefOut(
        site_id=site.site_id, name=site.name, created_at=site.created_at,
        map_count=0, robot_count=0,
    )


@router.get("", response_model=list[SiteBriefOut])
def list_sites(request: Request) -> list[SiteBriefOut]:
    registry = _registry(request)
    out = []
    for site in registry.list_sites():
        try:
            map_count = len(registry.map_store(site.site_id).list_maps())
        except Exception:
            map_count = 0
        out.append(SiteBriefOut(
            site_id=site.site_id, name=site.name, created_at=site.created_at,
            map_count=map_count, robot_count=len(site.robots),
        ))
    return out


@router.get("/{site_id}", response_model=SiteOut)
def get_site(site_id: str, request: Request) -> SiteOut:
    registry = _registry(request)
    try:
        site = registry.get_site(site_id)
        maps = registry.map_store(site_id).list_maps()
    except SiteError as exc:
        raise _404(str(exc)) from None
    return SiteOut(
        site_id=site.site_id,
        name=site.name,
        created_at=site.created_at,
        maps=[_map_summary_out(m) for m in maps],
        robots=[_assignment_out(a) for a in site.robots.values()],
    )


@router.delete("/{site_id}")
def delete_site(site_id: str, request: Request, force: bool = False):
    try:
        _registry(request).delete_site(site_id, force=force)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"deleted": site_id}


# =============================================================================
# Map
# =============================================================================

@router.post("/{site_id}/maps", response_model=IngestResultOut)
async def upload_map(
    site_id: str,
    request: Request,
    map_id: str = Form(..., description="맵 식별자 (물리 구역 이름 권장: floor-1 등)"),
    despeckle_iterations: int = Form(0),
    activate: bool = Form(False),
    files: list[UploadFile] = None,  # noqa: RUF013
) -> IngestResultOut:
    registry = _registry(request)
    try:
        store = registry.map_store(site_id)
    except SiteError as exc:
        raise _404(str(exc)) from None
    if not files:
        raise HTTPException(status_code=422, detail="파일이 없다 (벤더 폴더 파일들을 올려라)")

    payload: dict[str, bytes] = {}
    for f in files:
        if f.filename:
            payload[f.filename] = await f.read()

    try:
        result = store.ingest_upload(
            map_id, payload,
            despeckle_iterations=despeckle_iterations, activate=activate,
        )
    except MapStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    notified: list[str] = []
    if activate:
        notified = _coordinator(request).notify_map_activated(site_id, map_id)

    return IngestResultOut(
        site_id=site_id, map_id=result.map_id, map_uid=result.map_uid, version=result.version,
        created=result.created, activated=activate, notified_robots=notified,
        warnings=result.warnings, stats=result.stats,
    )


@router.get("/{site_id}/maps", response_model=list[MapSummaryOut])
def list_maps(site_id: str, request: Request) -> list[MapSummaryOut]:
    try:
        store = _registry(request).map_store(site_id)
    except SiteError as exc:
        raise _404(str(exc)) from None
    return [_map_summary_out(m) for m in store.list_maps()]


@router.get("/{site_id}/maps/{map_id}", response_model=MapSummaryOut)
def get_map(site_id: str, map_id: str, request: Request) -> MapSummaryOut:
    try:
        store = _registry(request).map_store(site_id)
        return _map_summary_out(store.get_map(map_id))
    except (SiteError, MapStoreError) as exc:
        raise _404(str(exc)) from None


@router.get("/{site_id}/maps/{map_id}/manifest")
def get_manifest(site_id: str, map_id: str, request: Request, version: str | None = None):
    try:
        store = _registry(request).map_store(site_id)
        return JSONResponse(store.manifest(map_id, version))
    except (SiteError, MapStoreError) as exc:
        raise _404(str(exc)) from None


@router.get("/{site_id}/maps/{map_id}/bundle")
def get_bundle(site_id: str, map_id: str, request: Request, version: str | None = None):
    try:
        store = _registry(request).map_store(site_id)
        resolved = store.resolve_version(map_id, version)
        data = store.bundle_zip(map_id, resolved)
    except (SiteError, MapStoreError) as exc:
        raise _404(str(exc)) from None
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{site_id}-{map_id}-{resolved}.zip"',
            "X-Map-Version": resolved,
        },
    )


@router.get("/{site_id}/maps/{map_id}/files/{filename}")
def get_file(site_id: str, map_id: str, filename: str, request: Request, version: str | None = None):
    try:
        store = _registry(request).map_store(site_id)
        path = store.file_path(map_id, filename, version)
    except (SiteError, MapStoreError) as exc:
        raise _404(str(exc)) from None
    return FileResponse(path)


@router.post("/{site_id}/maps/{map_id}/activate", response_model=ActivateResultOut)
def activate_map(site_id: str, map_id: str, body: ActivateRequest, request: Request) -> ActivateResultOut:
    try:
        store = _registry(request).map_store(site_id)
        store.activate(map_id, body.version)
    except (SiteError, MapStoreError) as exc:
        raise _404(str(exc)) from None

    notified: list[str] = []
    if body.notify:
        notified = _coordinator(request).notify_map_activated(site_id, map_id)
    return ActivateResultOut(
        site_id=site_id, map_id=map_id, version=body.version, notified_robots=notified
    )


# =============================================================================
# Robot
# =============================================================================

@router.post("/{site_id}/robots", response_model=RegisterRobotResultOut)
def register_robot(site_id: str, body: RegisterRobotRequest, request: Request) -> RegisterRobotResultOut:
    try:
        assignment = _registry(request).register_robot(site_id, body.robot_id, body.map_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    sent = _coordinator(request).notify_robot(body.robot_id)
    return RegisterRobotResultOut(assignment=_assignment_out(assignment), set_map_sent=sent)


@router.get("/{site_id}/robots", response_model=list[RobotAssignmentOut])
def list_robots(site_id: str, request: Request) -> list[RobotAssignmentOut]:
    try:
        site = _registry(request).get_site(site_id)
    except SiteError as exc:
        raise _404(str(exc)) from None
    return [_assignment_out(a) for a in site.robots.values()]


@router.put("/{site_id}/robots/{robot_id}", response_model=RegisterRobotResultOut)
def reassign_robot(
    site_id: str, robot_id: str, body: ReassignRobotRequest, request: Request
) -> RegisterRobotResultOut:
    try:
        assignment = _registry(request).register_robot(site_id, robot_id, body.map_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    sent = _coordinator(request).notify_robot(robot_id)
    return RegisterRobotResultOut(assignment=_assignment_out(assignment), set_map_sent=sent)


@router.delete("/{site_id}/robots/{robot_id}")
def unregister_robot(site_id: str, robot_id: str, request: Request):
    try:
        _registry(request).unregister_robot(site_id, robot_id)
    except SiteError as exc:
        raise _404(str(exc)) from None
    return {"unregistered": robot_id, "site_id": site_id}
