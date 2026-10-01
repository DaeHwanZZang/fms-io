"""Named location REST 라우터.

관리자가 `(x, y, theta)` 에 이름을 붙인다. site 스코프 — 이름은 site 전체에서
유일하고, location 마다 어느 맵 프레임인지(`map_id`)를 함께 갖는다. job 은 나중에
raw 좌표 대신 이 이름으로 목적지를 지정한다.

    GET    /api/v1/sites/{site_id}/locations
    POST   /api/v1/sites/{site_id}/locations              {name, map_id, x, y, theta?, type?}
    GET    /api/v1/sites/{site_id}/locations/{name}
    PUT    /api/v1/sites/{site_id}/locations/{name}        부분 수정 (준 필드만)
    DELETE /api/v1/sites/{site_id}/locations/{name}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fms_server.locations import LocationError, LocationNotFound, NamedLocation
from fms_server.sites import SiteError, SiteNotFound

log = logging.getLogger("fms.api.locations")

router = APIRouter(prefix="/api/v1/sites/{site_id}/locations", tags=["locations"])


# =============================================================================
# 모델
# =============================================================================

class CreateLocationRequest(BaseModel):
    name: str
    map_id: str
    x: float
    y: float
    theta: float = 0.0
    type: str = "waypoint"
    keep_theta: bool = False   # [homework_iot 추가]


class UpdateLocationRequest(BaseModel):
    map_id: str | None = None
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    type: str | None = None
    keep_theta: bool | None = None   # [homework_iot 추가]


class LocationOut(BaseModel):
    name: str
    map_id: str
    map_uid: str
    x: float
    y: float
    theta: float
    type: str
    keep_theta: bool = False
    created_at: str
    updated_at: str
    warnings: list[str] = []


# =============================================================================
# 헬퍼
# =============================================================================

def _store(request: Request, site_id: str):
    try:
        return request.app.state.sites.location_store(site_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _out(request: Request, site_id: str, loc: NamedLocation, warnings: list[str] | None = None) -> LocationOut:
    try:
        map_uid = request.app.state.sites.map_store(site_id).map_uid(loc.map_id)
    except Exception:
        map_uid = ""
    return LocationOut(
        name=loc.name, map_id=loc.map_id, map_uid=map_uid,
        x=loc.x, y=loc.y, theta=loc.theta, type=loc.type, keep_theta=loc.keep_theta,
        created_at=loc.created_at, updated_at=loc.updated_at,
        warnings=warnings or [],
    )


# =============================================================================
# 라우트
# =============================================================================

@router.get("", response_model=list[LocationOut])
def list_locations(site_id: str, request: Request) -> list[LocationOut]:
    store = _store(request, site_id)
    return [_out(request, site_id, loc) for loc in store.list()]


@router.post("", response_model=LocationOut)
def create_location(site_id: str, body: CreateLocationRequest, request: Request) -> LocationOut:
    store = _store(request, site_id)
    try:
        loc, warnings = store.create(
            body.name, body.map_id, body.x, body.y, body.theta, body.type, body.keep_theta
        )
    except LocationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(request, site_id, loc, warnings)


@router.get("/{name}", response_model=LocationOut)
def get_location(site_id: str, name: str, request: Request) -> LocationOut:
    store = _store(request, site_id)
    try:
        return _out(request, site_id, store.get(name))
    except LocationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.put("/{name}", response_model=LocationOut)
def update_location(
    site_id: str, name: str, body: UpdateLocationRequest, request: Request
) -> LocationOut:
    store = _store(request, site_id)
    try:
        loc, warnings = store.update(
            name, map_id=body.map_id, x=body.x, y=body.y, theta=body.theta, type=body.type,
            keep_theta=body.keep_theta,
        )
    except LocationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except LocationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(request, site_id, loc, warnings)


@router.delete("/{name}")
def delete_location(site_id: str, name: str, request: Request):
    store = _store(request, site_id)
    try:
        store.delete(name)
    except LocationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"deleted": name, "site_id": site_id}
