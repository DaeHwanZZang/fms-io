#!/bin/zsh
# 라즈베리파이로 파일 전송 (scp)
#   ./send_to_pi.sh                 -> server/ 와 web/ 전송 (기본)
#   ./send_to_pi.sh 파일 폴더 ...   -> 지정한 것만 전송
# 환경변수로 덮어쓰기: PI_HOST, PI_USER, PI_DEST

PI_HOST="${PI_HOST:-172.20.10.5}"
PI_USER="${PI_USER:-pi}"
PI_DEST="${PI_DEST:-/home/pi/Downloads}"

cd "$(dirname "$0")" || exit 1

if [[ $# -gt 0 ]]; then
  targets=("$@")
else
  targets=(server web)
fi

# 존재하지 않는 대상 미리 걸러냄
for t in "${targets[@]}"; do
  if [[ ! -e "$t" ]]; then
    echo "없음: $t" >&2
    exit 1
  fi
done

# 로컬 캐시(__pycache__)는 보내지 않는다
find "${targets[@]}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null

echo "전송: ${targets[*]} -> ${PI_USER}@${PI_HOST}:${PI_DEST}"
scp -r "${targets[@]}" "${PI_USER}@${PI_HOST}:${PI_DEST}/" \
  && echo "완료" \
  || { echo "실패 (파이 켜짐/같은 네트워크/SSH 활성화 확인)" >&2; exit 1; }
