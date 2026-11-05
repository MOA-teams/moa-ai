#!/usr/bin/env bash
# 서버(~/moa-ai)에서 실행되는 배포 스크립트. GitHub Actions가 SSH로 호출한다.
# 표준입력으로 .env 내용(ENV_FILE secret)을 받아 프로젝트 .env에 기록한다.
set -euo pipefail

APP_DIR="${MOA_APP_DIR:-$HOME/moa-ai}"
SERVICE="moa-ai"
PORT="${MOA_AI_PORT:-8001}"

cd "$APP_DIR"

# git/pip가 SSH stdin을 소비하기 전에 .env payload를 안전한 임시 파일로 받는다.
ENV_PAYLOAD=""
if [ ! -t 0 ]; then
  ENV_PAYLOAD=$(mktemp)
  trap 'rm -f "$ENV_PAYLOAD"' EXIT
  cat > "$ENV_PAYLOAD"
fi

git fetch --quiet origin main
git reset --hard origin/main

# .env 기록: payload가 비어있지 않을 때만 갱신(빈 전달로 기존 .env를 날리지 않도록).
if [ -n "$ENV_PAYLOAD" ] && [ -s "$ENV_PAYLOAD" ]; then
  install -m 600 "$ENV_PAYLOAD" "$APP_DIR/.env"
  echo "env written"
elif [ -f "$APP_DIR/.env" ]; then
  echo "env unchanged (existing .env kept)"
else
  echo "no .env provided and none present" >&2
  exit 1
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# RAG용 Chroma 저장 경로 보장 (.env의 MOA_CHROMA_PATH 기본값 .data/chroma)
CHROMA_PATH=$(sed -n 's/^MOA_CHROMA_PATH=//p' "$APP_DIR/.env" | head -1)
CHROMA_PATH="${CHROMA_PATH:-.data/chroma}"
case "$CHROMA_PATH" in
  /*) mkdir -p "$CHROMA_PATH" ;;
  *)  mkdir -p "$APP_DIR/$CHROMA_PATH" ;;
esac

sudo systemctl restart "$SERVICE"

# 헬스체크: 기동될 때까지 최대 ~20초 대기
for _ in $(seq 1 10); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/health"; then
    echo "deploy ok ($(git rev-parse --short HEAD))"
    exit 0
  fi
  sleep 2
done

echo "health check failed" >&2
sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
