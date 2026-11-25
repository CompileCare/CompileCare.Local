#!/bin/bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
docker compose --env-file local/.env -f local/docker-compose.yml down
echo "CompileCare stopped. PostgreSQL volume retained."
