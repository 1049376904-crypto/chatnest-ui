#!/usr/bin/env bash
# 开发用。生产用 systemd，见 server/README.md。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f server/.env ]; then
  echo "没有 server/.env。先：cp server/env.example server/.env" >&2
  exit 1
fi

# 只听 127.0.0.1。对外用 nginx 反代，不要直接把它摆到公网。
exec uvicorn server.main:app --host 127.0.0.1 --port "${PORT:-8787}"
