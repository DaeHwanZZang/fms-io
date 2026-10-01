"""
라즈베리파이(io_server) 출력 제어 클라이언트 ([homework_iot 추가])
================================================================

job 의 `set_output` 명령이 `POST {pi_url}/api/output {name, value}` 로 핀 출력을 시킨다.
표준 라이브러리만 쓴다. 실패(연결 불가/거부)는 `IoError` — job 이 failed 로 끝난다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class IoError(Exception):
    pass


class PiIoClient:
    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout

    def set_output(self, device: str, value: int) -> None:
        req = urllib.request.Request(
            f"{self._url}/api/output",
            data=json.dumps({"name": device, "value": int(value)}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("error", "")
            except Exception:
                detail = ""
            raise IoError(f"라즈베리파이가 {device} 제어를 거부했다: {detail or exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise IoError(f"라즈베리파이({self._url})에 연결할 수 없다: {getattr(exc, 'reason', exc)}") from None
