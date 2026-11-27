# Cross-repository full-stack verification — 2026-11-27

## Purpose

Re-verify the current CompileCare API, authenticated Web workspace and shared observability packages together in the isolated nine-service Docker Compose environment. This is a synthetic-data CI acceptance run, not a production or pilot deployment.

## Repository baseline

The Local workflow checks out the current main branches of:

- CompileCare.Local — Compose orchestration and acceptance scripts.
- CompileCare.Api — .NET API, PostgreSQL persistence, tenant security and hospital workflows.
- CompileCare.Web — authenticated React/TypeScript workspace.
- CompileCare.Observability — shared .NET observability libraries packed into an ephemeral local NuGet feed.

The exact commit SHAs checked out by the run must be recorded in the completed evidence section below. Do not infer them from this document's commit.

## Automated acceptance scope

1. Validate Compose configuration and build the API and Web containers.
2. Start the isolated nine-service application and observability stack:
   - Keycloak
   - PostgreSQL
   - CompileCare API
   - CompileCare Web
   - OpenTelemetry Collector
   - Prometheus
   - Jaeger
   - Elasticsearch
   - Kibana
3. Reconcile the local Keycloak client without resetting the realm or deleting local users.
4. Verify infrastructure readiness and telemetry pipeline behavior.
5. Send synthetic telemetry and verify:
   - OTLP metrics are queryable in Prometheus.
   - OTLP traces are queryable in Jaeger.
   - structured synthetic logs are queryable in Elasticsearch.
   - Kibana data view is provisioned.
6. Obtain real Keycloak-issued access tokens in the isolated CI realm.
7. Bootstrap a temporary platform operator and create two synthetic tenants.
8. Verify issuer/audience validation, persisted tenant membership, role-derived permissions, tenant-specific data isolation, cross-tenant membership mutation denial, membership revocation/reactivation and current-user tenant-context responses against PostgreSQL.
9. Run real browser sign-in using Authorization Code + PKCE against the local Keycloak realm.
10. Tear down the isolated Compose project and remove CI-only volumes, credentials and browser identity artifacts even on failure.

## Security and data constraints

- Use only ephemeral CI credentials and synthetic tenant/user data.
- Do not use real patient, clinical, billing or production records.
- Do not enable the CI-only password-grant client in the checked-in browser client.
- Do not run the CI acceptance script against a persistent developer environment or production.
- Retain failure diagnostics without exposing secrets or patient information.

## Evidence record

**Status:** Two hosted full-stack acceptance runs passed on 2026-11-27. The second run verified the post-PR #65 Web baseline and recorded exact cross-repository commit SHAs.

Record after the run completes:

- GitHub Actions run URL and run ID.
- Local, API, Web and Observability commit SHAs checked out.
- Full nine-service job result.
- Infrastructure and telemetry result.
- Real Keycloak/persisted multi-tenant result.
- Real OIDC browser result.
- Cleanup result.

## Explicitly outside this automated run

- Manual one-click launcher acceptance on a developer Mac and Windows 10/11 machine.
- Manual keyboard-only, screen-reader, zoom, contrast and full WCAG 2.2 AA assessment.
- Provider-native PostgreSQL physical backup/PITR and isolated production restore drill.
- Production ingress, DNS, TLS, secret-management and signing-key rotation rehearsal.
- Clinical governance, privacy/legal review and hospital pilot sign-off.

A successful CI run must not be described as production approval or as closure of these manual/operational gates.


### Completed hosted run — 2026-11-27

- Run: [CompileCare.Local full-stack verification #45944486631](https://github.com/CompileCare/CompileCare.Local/actions/runs/45944486631)
- Result: **Success**. The full nine-service Compose stack built and started; the synthetic local stack smoke passed; real Keycloak-issued tokens passed API issuer/audience validation; persisted multi-tenant workflows passed; real OIDC Authorization Code + PKCE browser acceptance passed (1 test); isolated CI secrets and volumes were cleaned up successfully.
- The run began before Web PR #65 was merged, so it validates the route-level workspace baseline from Web PR #64. A follow-up run is triggered by this PR to validate the extracted patient-registration component on the current Web main branch.
- Exact checked-out API/Web/Observability SHAs were not explicitly printed by the first run. This PR adds a dedicated commit-SHA recording step before the next full-stack build; the next run's evidence record will capture those values rather than infer them from merge timestamps.


### Completed current-main run with recorded repository SHAs — 2026-11-27

- Run: [CompileCare.Local full-stack verification #45945066526](https://github.com/CompileCare/CompileCare.Local/actions/runs/45945066526)
- Full job: [Full nine-service Docker stack and API telemetry verification](https://github.com/CompileCare/CompileCare.Local/actions/runs/45945066526/job/140327775505) — **Success**.
- Exact checked-out commits:
  - CompileCare.Local: `fa049b0a687f1af3778cc8a680bc00188381ec16`
  - CompileCare.Api: `0a8f326cea2be23984a68e90e100188b311b7a4b`
  - CompileCare.Web: `fe11922463fce11404b4d8efad3717b70115863d`
  - CompileCare.Observability: `935f6629d5abb92296385ec2e0c1ec04060b3fb7`
- Infrastructure/telemetry: Keycloak OIDC discovery, Prometheus, Jaeger, Elasticsearch, OpenTelemetry Collector and Kibana readiness passed; the Kibana data view was provisioned. API traces, Npgsql database spans and Collector-fed ASP.NET Core metrics were verified end-to-end.
- Identity/authorization: real Keycloak-issued tokens passed issuer/audience validation; two PostgreSQL-persisted tenant memberships with different roles were verified; hospital data isolation, cross-tenant membership mutation denial, membership revocation/reactivation and FacilityViewer patient-access denials passed.
- Browser: real Keycloak Authorization Code + PKCE browser sign-in passed (1 E2E test, 5.1 seconds) and loaded persisted tenant workspaces.
- Cleanup: GitHub Actions post-job cleanup completed successfully for the isolated job.

**Acceptance boundary:** this run verifies the synthetic Linux CI stack and browser authentication. It does not close manual Mac/Windows launcher acceptance, manual accessibility/screen-reader review, clinical workflow visual acceptance, production ingress/TLS, provider-native PostgreSQL PITR/restore, signing-key rotation or hospital pilot sign-off.
