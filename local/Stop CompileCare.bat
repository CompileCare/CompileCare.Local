@echo off
cd /d "%~dp0.."
docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml down
echo HeapCare and observability stopped. PostgreSQL, Keycloak, Prometheus and Elasticsearch volumes retained.
pause
