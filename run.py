"""FMS와 I/O 서버를 같은 터미널에서 실행한다. 종료: Ctrl+C."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".venv"
PYTHON = ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = [ROOT / "requirements.txt", ROOT / "server/requirements.txt"]


def prepare():
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 이상이 필요합니다. Python 3.12 또는 3.13을 권장합니다.")
    if not PYTHON.exists():
        print("가상환경 생성 중…", flush=True)
        venv.EnvBuilder(with_pip=True).create(ENV)
    fingerprint = hashlib.sha256(b"".join(p.read_bytes() for p in REQUIREMENTS)).hexdigest()
    marker = ENV / ".fms-requirements"
    if not marker.exists() or marker.read_text() != fingerprint:
        print("실행에 필요한 패키지 설치 중… (첫 실행에는 인터넷이 필요합니다)", flush=True)
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS[0]),
                        "-r", str(REQUIREMENTS[1])], check=True, cwd=ROOT)
        marker.write_text(fingerprint)


def check_ports():
    for port in (8000, 5001):
        with socket.socket() as sock:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"포트 {port}를 사용할 수 없습니다. 기존 서버를 종료하고 다시 실행하세요.") from exc


def ready(url):
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 생략")
    args = parser.parse_args()
    os.chdir(ROOT)
    check_ports()
    prepare()
    processes = []
    try:
        for command in ([str(PYTHON), "-m", "uvicorn", "fms_server.main:app", "--host", "127.0.0.1", "--port", "8000"],
                        [str(PYTHON), "server/io_server.py"]):
            processes.append(subprocess.Popen(command, cwd=ROOT))
        deadline = time.monotonic() + 30
        while not all(ready(url) for url in ("http://127.0.0.1:8000/health", "http://127.0.0.1:5001/")):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("서버가 시작 중 종료됐습니다. 위 오류 로그를 확인하세요.")
            if time.monotonic() > deadline:
                raise RuntimeError("서버 시작 대기 시간(30초)을 초과했습니다.")
            time.sleep(0.2)
        print("\nFMS 관제: http://localhost:8000/ui/fms.html\n설비 I/O: http://localhost:5001/\n종료: 이 창에서 Ctrl+C\n", flush=True)
        if not args.no_browser:
            webbrowser.open("http://localhost:8000/ui/fms.html")
            webbrowser.open("http://localhost:5001/")
        while True:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("서버 하나가 종료되어 나머지 서버도 정리합니다.")
            time.sleep(0.5)
    finally:
        # Ctrl+C가 자식에게도 전달된 경우 먼저 정상 종료를 기다린다.
        for process in processes:
            if process.poll() is None:
                if os.name != "nt":
                    process.send_signal(signal.SIGINT)
                else:
                    process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n서버를 종료했습니다.")
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"\n실행 실패: {error}", file=sys.stderr)
        sys.exit(1)
