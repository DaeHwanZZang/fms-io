"""job (작업) REST 라우터.

job = 로봇이 수행할 명령의 순서 묶음 (지금은 move 만). site 스코프.
규약은 `../task.py` 참고.

    POST   /api/v1/sites/{site_id}/jobs                {commands:[{type:"move",target:"<loc>"} | {type:"wait",seconds} | {type:"wait_flag",flag,until?} | {type:"set_output",device,value}], robot_id?}
    GET    /api/v1/sites/{site_id}/jobs
    GET    /api/v1/sites/{site_id}/jobs/{job_id}
    POST   /api/v1/sites/{site_id}/jobs/{job_id}/dispatch   {robot_id?}
    POST   /api/v1/sites/{site_id}/jobs/{job_id}/cancel
    DELETE /api/v1/sites/{site_id}/jobs/{job_id}            (working 이 아닐 때만)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fms_server.sites import SiteError, SiteNotFound
from fms_server.task import Job, JobError, JobNotFound

log = logging.getLogger("fms.api.jobs")

router = APIRouter(prefix="/api/v1/sites/{site_id}/jobs", tags=["jobs"])


# =============================================================================
# 모델
# =============================================================================

class CommandIn(BaseModel):
    type: str = "move"
    target: str | None = None    # move
    seconds: float | None = None   # wait   [homework_iot 추가]
    flag: str | None = None        # wait_flag [homework_iot 추가]
    device: str | None = None      # set_output: 파이 출력 장치 이름 (lamp_red 등)
    value: int | None = None       # set_output: 0 | 1
    hold: float | None = None      # wait_flag: 조건 유지 시간(초)
    timeout: float | None = None   # wait_flag: 대기 제한(초)
    on_timeout: str | None = None  # wait_flag: fail(기본) | continue
    until: str | None = None       # wait_flag: lowered(기본) | raised


class CreateJobRequest(BaseModel):
    commands: list[CommandIn]
    robot_id: str | None = None


class DispatchRequest(BaseModel):
    robot_id: str | None = None


class JobOut(BaseModel):
    job_id: str
    site_id: str
    status: str
    robot_id: str | None
    commands: list[dict]
    order_id: str | None
    order_update_id: int
    done_commands: int
    total_commands: int
    phase: str | None
    cursor: int
    wait_started: float | None
    error: str | None
    created_at: str
    updated_at: str
    dispatched_at: str | None

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls(
            job_id=job.job_id,
            site_id=job.site_id,
            status=job.status.value,
            robot_id=job.robot_id,
            commands=[c.to_dict() for c in job.commands],
            order_id=job.order_id,
            order_update_id=job.order_update_id,
            done_commands=job.done_commands,
            total_commands=len(job.commands),
            phase=job.phase,
            cursor=job.cursor,
            wait_started=job.wait_started,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            dispatched_at=job.dispatched_at,
        )


# =============================================================================
# 헬퍼
# =============================================================================

def _store(request: Request):
    return request.app.state.jobs


def _coordinator(request: Request):
    return request.app.state.job_coordinator


def _check_site(request: Request, site_id: str) -> None:
    try:
        request.app.state.sites.get_site(site_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _job_in_site(request: Request, site_id: str, job_id: str) -> Job:
    try:
        job = _store(request).get(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if job.site_id != site_id:
        raise HTTPException(status_code=404, detail=f"job {job_id} 는 site {site_id} 에 없다")
    return job


# =============================================================================
# 라우트
# =============================================================================

@router.post("", response_model=JobOut)
def create_job(site_id: str, body: CreateJobRequest, request: Request) -> JobOut:
    _check_site(request, site_id)
    try:
        job = _store(request).create(
            site_id, [c.model_dump(exclude_none=True) for c in body.commands], body.robot_id
        )
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.get("", response_model=list[JobOut])
def list_jobs(site_id: str, request: Request) -> list[JobOut]:
    _check_site(request, site_id)
    return [JobOut.of(j) for j in _store(request).list(site_id)]


@router.get("/{job_id}", response_model=JobOut)
def get_job(site_id: str, job_id: str, request: Request) -> JobOut:
    return JobOut.of(_job_in_site(request, site_id, job_id))


@router.post("/{job_id}/dispatch", response_model=JobOut)
def dispatch_job(site_id: str, job_id: str, body: DispatchRequest, request: Request) -> JobOut:
    _job_in_site(request, site_id, job_id)
    try:
        job = _coordinator(request).dispatch(job_id, body.robot_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(site_id: str, job_id: str, request: Request) -> JobOut:
    _job_in_site(request, site_id, job_id)
    try:
        job = _coordinator(request).cancel(job_id)
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.delete("/{job_id}")
def delete_job(site_id: str, job_id: str, request: Request):
    _job_in_site(request, site_id, job_id)
    try:
        _store(request).delete(job_id)
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"deleted": job_id, "site_id": site_id}
