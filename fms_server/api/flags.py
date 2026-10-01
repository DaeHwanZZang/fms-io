"""외부 플래그 REST ([homework_iot 추가]). 규약은 `../flags.py` 참고.

    GET /api/v1/flags
    PUT /api/v1/flags/{name}   {value: bool}      (라즈베리파이가 신호를 올리고 내릴 때)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fms_server.flags import FlagError

router = APIRouter(prefix="/api/v1/flags", tags=["flags"])


class FlagIn(BaseModel):
    value: bool


@router.get("")
def list_flags(request: Request) -> dict:
    return request.app.state.flags.snapshot()


@router.put("/{name}")
def set_flag(name: str, body: FlagIn, request: Request) -> dict:
    try:
        request.app.state.flags.set(name, body.value)
    except FlagError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"name": name, "value": body.value}
