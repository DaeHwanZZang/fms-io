#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 이상을 python.org에서 설치한 뒤 다시 실행하세요."
  read '?Enter를 누르면 닫힙니다.'
  exit 1
fi
python3 run.py
result=$?
if (( result != 0 )); then
  read '?오류를 확인한 뒤 Enter를 누르세요.'
fi
exit $result
