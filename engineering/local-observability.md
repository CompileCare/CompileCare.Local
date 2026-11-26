# CompileCare local observability

This optional Compose overlay connects CompileCare.Api to the shared observability components for local development. It is intended for synthetic/test data only.

## Start the full local stack with observability

From the CompileCare.Local repository root:

```bash
docker compose --env-file local/.env \
  -f local/docker-compose.yml \
  -f local/docker-compose.observability.yml \
  up --build -d
```

The overlay leaves the standard local stack unchanged when it is not supplied. It pins the OpenTelemetry Collector to 0.165.0, Jaeger to 2.23.0 and Prometheus to 3.16.0 so local telemetry behavior is reproducible. The API OTLP exporter uses gRPC on the collector receiver at otel-collector:4317; the Collector uses the current `otlp_http` exporter and an explicit Jaeger traces endpoint (`http://jaeger:4318/v1/traces`). The overlay also configures the existing Serilog Elasticsearch sink.

## Where to view telemetry

| Signal | UI / endpoint | What to use it for |
|---|---|---|
| Structured logs | http://localhost:5601 (Kibana) | Create a data view for compilecare-logs-* and search by service, environment, correlation ID or audit action |
| Metrics | http://localhost:9090 (Prometheus) | Query API and collector metrics; inspect the compilecare-api and compilecare-otel-collector scrape targets |
| Distributed traces | http://localhost:16686 (Jaeger) | Select the CompileCare.Api service and inspect request traces |
| Raw API metrics | http://localhost:8081/metrics | Verify the API Prometheus scrape endpoint directly |
| Collector OTLP gRPC | http://localhost:4317 | OTLP gRPC receiver used by CompileCare.Api |
| Collector OTLP HTTP | http://localhost:4318 | OTLP HTTP Protobuf receiver for compatible local/test senders |
| API health | http://localhost:8081/health/live and http://localhost:8081/health/ready | Verify process and dependency health |

The API writes structured Serilog events to stdout and Elasticsearch when ElasticConfiguration:Uri is set. Docker logs remain available as a fallback:

```bash
docker compose --env-file local/.env \
  -f local/docker-compose.yml \
  -f local/docker-compose.observability.yml logs -f api
```

## Verify the pipeline

1. Open the API root and health endpoints; confirm the API responds.
2. Call a harmless API endpoint and note its X-Correlation-ID response header.
3. Confirm request metrics appear in Prometheus after the scrape interval.
4. Find the corresponding CompileCare.Api service trace in Jaeger.
5. Search Elasticsearch/Kibana for the request log and use the correlation ID where present.
6. Confirm no patient payloads, access tokens, credentials or other PHI are entered into test data or saved in screenshots/reports.

The OTLP Collector exports metrics to its Prometheus exporter and traces to Jaeger. Logs are sent by the API Serilog Elasticsearch sink; this setup does not add an OTLP logs pipeline.

## Resource and safety notes

- Elasticsearch/Kibana add significant memory and CPU overhead. Allocate adequate Docker Desktop resources and stop the overlay when it is not needed.
- Elasticsearch security is disabled only for this loopback-bound local development stack. Never copy this configuration to a shared or production environment.
- This stack is not a production observability deployment: it has no durable alerting/SLO policy, authentication, TLS, backup/retention policy or high availability.
- Use synthetic patient/test data only. Do not use production patient records or credentials.
- To stop containers while preserving data, use Compose stop. Do not use down --volumes unless intentionally deleting local PostgreSQL, Keycloak, Prometheus and Elasticsearch data.
