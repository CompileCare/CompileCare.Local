#!/bin/bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml down
echo "CompileCare and observability stopped. PostgreSQL, Keycloak, Prometheus and Elasticsearch volumes retained."
