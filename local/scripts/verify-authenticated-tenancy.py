#!/usr/bin/env python3
"""CI-only real-Keycloak and PostgreSQL multi-tenant acceptance test.

This test deliberately uses an ephemeral Compose stack and a temporary Keycloak
client with the password grant enabled. It does not modify the production
compilecare-web client or production identity-provider settings.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = LOCAL_ROOT / ".env"
KEYCLOAK = "http://keycloak.localhost:8082"
REALM = "compilecare"
API = "http://localhost:8081"
CI_CLIENT_ID = "heapcare-ci-acceptance"


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise RuntimeError("Keycloak returned a token that is not a JWT.")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Any = None,
    form: dict[str, str] | None = None,
    token: str | None = None,
) -> tuple[int, Any, dict[str, str]]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
            return response.status, body, dict(response.headers.items())
    except urllib.error.HTTPError as error:
        raw = error.read()
        content_type = error.headers.get("Content-Type", "")
        body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
        return error.code, body, dict(error.headers.items())


def require_status(
    action: str,
    result: tuple[int, Any, dict[str, str]],
    expected: int,
) -> Any:
    status, body, _ = result
    if status != expected:
        raise AssertionError(
            f"{action}: expected HTTP {expected}, received HTTP {status}; response={body!r}"
        )
    return body


class KeycloakAdmin:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, payload: Any = None) -> tuple[int, Any, dict[str, str]]:
        return request(self.base_url, path, method=method, payload=payload, token=self.token)


class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        token: str,
        tenant_id: str | None = None,
        payload: Any = None,
    ) -> tuple[int, Any, dict[str, str]]:
        headers = {"Authorization": f"Bearer {token}"}
        if tenant_id is not None:
            headers["X-Tenant-ID"] = tenant_id
        return self._request(method, path, headers, payload)

    def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        payload: Any,
    ) -> tuple[int, Any, dict[str, str]]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        outgoing = {"Accept": "application/json", **headers}
        if data is not None:
            outgoing["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=outgoing, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
                return response.status, body, dict(response.headers.items())
        except urllib.error.HTTPError as error:
            raw = error.read()
            content_type = error.headers.get("Content-Type", "")
            body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
            return error.code, body, dict(error.headers.items())


def admin_access_token(username: str, password: str) -> str:
    result = request(
        KEYCLOAK,
        "/realms/master/protocol/openid-connect/token",
        method="POST",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": username,
            "password": password,
        },
    )
    body = require_status("Keycloak master admin login", result, 200)
    token = body.get("access_token")
    if not token:
        raise RuntimeError("Keycloak admin login returned no access token.")
    return token


def create_ci_client(admin: KeycloakAdmin) -> str:
    scopes = require_status(
        "Read HeapCare client scopes",
        admin.call("GET", f"/admin/realms/{REALM}/client-scopes"),
        200,
    )
    audience_scope = next(
        (scope for scope in scopes if scope.get("name") == "compilecare-api-audience"),
        None,
    )
    basic_scope = next(
        (scope for scope in scopes if scope.get("name") == "basic"),
        None,
    )
    if audience_scope is None:
        raise RuntimeError("The HeapCare API audience client scope is missing.")
    if basic_scope is None:
        raise RuntimeError("The Keycloak basic client scope is missing; access tokens require its Subject (sub) mapper.")

    basic_mappers = require_status(
        "Read Keycloak basic scope protocol mappers",
        admin.call(
            "GET",
            f"/admin/realms/{REALM}/client-scopes/{basic_scope['id']}/protocol-mappers/models",
        ),
        200,
    )
    if not any(
        mapper.get("protocolMapper") == "oidc-sub-mapper"
        and mapper.get("config", {}).get("access.token.claim") == "true"
        for mapper in basic_mappers
    ):
        raise RuntimeError("The Keycloak basic scope is missing its access-token Subject (sub) mapper.")

    status, body, _ = admin.call(
        "POST",
        f"/admin/realms/{REALM}/clients",
        {
            "clientId": CI_CLIENT_ID,
            "name": "HeapCare ephemeral CI acceptance client",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": False,
        },
    )
    if status != 201:
        raise AssertionError(
            f"Create temporary Keycloak CI client: expected HTTP 201, received HTTP {status}; response={body!r}"
        )

    clients = require_status(
        "Find temporary Keycloak CI client",
        admin.call(
            "GET",
            f"/admin/realms/{REALM}/clients?clientId={urllib.parse.quote(CI_CLIENT_ID)}",
        ),
        200,
    )
    if len(clients) != 1:
        raise RuntimeError("Expected exactly one temporary CI client in Keycloak.")
    client_uuid = clients[0]["id"]

    for label, scope in (
        ("basic subject", basic_scope),
        ("API audience", audience_scope),
    ):
        require_status(
            f"Assign {label} scope to temporary CI client",
            admin.call(
                "PUT",
                f"/admin/realms/{REALM}/clients/{client_uuid}/default-client-scopes/{scope['id']}",
            ),
            204,
        )
    return client_uuid


def create_user(admin: KeycloakAdmin, username: str, password: str) -> str:
    payload = {
        "username": username,
        "enabled": True,
        "email": f"{username}@heapcare.invalid",
        "emailVerified": True,
        "firstName": "HeapCare",
        "lastName": "CI Acceptance",
        "credentials": [
            {"type": "password", "value": password, "temporary": False}
        ],
    }
    require_status(
        f"Create synthetic Keycloak user {username}",
        admin.call("POST", f"/admin/realms/{REALM}/users", payload),
        201,
    )
    users = require_status(
        f"Find synthetic Keycloak user {username}",
        admin.call(
            "GET",
            f"/admin/realms/{REALM}/users?username={urllib.parse.quote(username)}&exact=true",
        ),
        200,
    )
    if len(users) != 1:
        raise RuntimeError(f"Expected exactly one Keycloak user for {username}.")
    return users[0]["id"]


def user_access_token(username: str, password: str, expected_issuer: str) -> tuple[str, str]:
    result = request(
        KEYCLOAK,
        f"/realms/{REALM}/protocol/openid-connect/token",
        method="POST",
        form={
            "grant_type": "password",
            "client_id": CI_CLIENT_ID,
            "username": username,
            "password": password,
        },
    )
    body = require_status(f"Obtain real Keycloak token for {username}", result, 200)
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"Keycloak returned no access token for {username}.")

    claims = decode_jwt_payload(token)
    if claims.get("iss") != expected_issuer:
        raise AssertionError(
            f"Keycloak issuer mismatch for {username}: {claims.get('iss')!r}"
        )
    audiences = claims.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if "compilecare-api" not in audiences:
        raise AssertionError(
            f"Keycloak access token for {username} is missing the HeapCare API audience."
        )
    subject = claims.get("sub")
    if not subject:
        raise AssertionError(f"Keycloak access token for {username} has no subject.")
    return token, subject


def create_tenant(api: Api, platform_token: str, name: str, slug: str) -> str:
    body = require_status(
        f"Create {name}",
        api.call(
            "POST",
            "/api/v1/platform/tenants",
            platform_token,
            payload={"name": name, "slug": slug},
        ),
        201,
    )
    return body["id"]


def provision_initial_admin(
    api: Api,
    platform_token: str,
    tenant_id: str,
    issuer: str,
    subject: str,
) -> str:
    body = require_status(
        "Provision initial tenant administrator",
        api.call(
            "POST",
            f"/api/v1/platform/tenants/{tenant_id}/initial-administrator",
            platform_token,
            payload={"issuer": issuer, "subject": subject},
        ),
        201,
    )
    return body["id"]


def set_tenant_active(api: Api, platform_token: str, tenant_id: str) -> None:
    require_status(
        "Activate provisioned tenant",
        api.call("POST", f"/api/v1/platform/tenants/{tenant_id}/activate", platform_token),
        200,
    )


def create_membership(
    api: Api,
    admin_token: str,
    tenant_id: str,
    issuer: str,
    subject: str,
    role: str,
) -> str:
    body = require_status(
        f"Create {role} membership",
        api.call(
            "POST",
            "/api/v1/tenant/memberships",
            admin_token,
            tenant_id,
            {
                "issuer": issuer,
                "subject": subject,
                "role": role,
                "scopes": [{"scopeType": "Tenant"}],
            },
        ),
        201,
    )
    return body["id"]


def create_hospital(api: Api, token: str, tenant_id: str, name: str, code: str) -> str:
    body = require_status(
        f"Create hospital {code}",
        api.call(
            "POST",
            "/api/v1/hospitals",
            token,
            tenant_id,
            {"name": name, "code": code},
        ),
        201,
    )
    return body["id"]


def create_department(api: Api, token: str, tenant_id: str, hospital_id: str, name: str, code: str) -> str:
    body = require_status(
        f"Create department {code}",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_id}/departments",
            token,
            tenant_id,
            {"name": name, "code": code},
        ),
        201,
    )
    return body["id"]


def expect_api_status(
    action: str,
    api: Api,
    method: str,
    path: str,
    token: str,
    tenant_id: str | None,
    expected: int,
    payload: Any = None,
) -> Any:
    return require_status(
        action,
        api.call(method, path, token, tenant_id, payload),
        expected,
    )


def run() -> None:
    if os.environ.get("HEAPCARE_CI_ACCEPTANCE") != "1":
        raise RuntimeError(
            "This script mutates Keycloak and PostgreSQL test data. "
            "Run it only from the isolated CI Compose workflow."
        )
    if not ENV_FILE.is_file():
        raise RuntimeError(f"Missing ephemeral environment file: {ENV_FILE}")

    env = read_env(ENV_FILE)
    admin_username = env.get("KEYCLOAK_ADMIN", "admin")
    admin_password = env.get("KEYCLOAK_ADMIN_PASSWORD", "")
    bootstrap_secret = env.get("PLATFORM_BOOTSTRAP_SECRET", "")
    if not admin_password or not bootstrap_secret:
        raise RuntimeError("The isolated CI environment is missing Keycloak or platform bootstrap credentials.")

    # Confirm discovery and issuer before creating any identity.
    discovery = require_status(
        "Read HeapCare OIDC discovery",
        request(KEYCLOAK, f"/realms/{REALM}/.well-known/openid-configuration"),
        200,
    )
    issuer = discovery["issuer"]
    if issuer != f"{KEYCLOAK}/realms/{REALM}":
        raise AssertionError(f"Unexpected OIDC issuer: {issuer!r}")

    admin_token = admin_access_token(admin_username, admin_password)
    admin = KeycloakAdmin(KEYCLOAK, admin_token)
    client_uuid = create_ci_client(admin)

    suffix = secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    usernames = {
        name: f"heapcare-ci-{name}-{suffix}"
        for name in ("platform", "tenant-a-admin", "tenant-b-admin", "shared", "facility-viewer", "facility-manager", "patient-registrar", "clinical-user")
    }
    subjects: dict[str, str] = {}
    tokens: dict[str, str] = {}
    for name, username in usernames.items():
        create_user(admin, username, password)
        tokens[name], subjects[name] = user_access_token(username, password, issuer)

    api = Api(API)

    # Bootstrap a persisted platform operator through the Development-only
    # endpoint, using a token actually issued and signed by local Keycloak.
    require_status(
        "Bootstrap platform operator using real Keycloak identity",
        api.call(
            "POST",
            "/api/v1/platform/bootstrap/operator",
            tokens["platform"],
            payload={"bootstrapSecret": bootstrap_secret},
        ),
        201,
    )
    operator_status = require_status(
        "Read persisted platform operator status",
        api.call("GET", "/api/v1/platform/operator/status", tokens["platform"]),
        200,
    )
    if operator_status.get("isPlatformOperator") is not True:
        raise AssertionError("The real Keycloak identity was not persisted as a platform operator.")

    tenant_a = create_tenant(api, tokens["platform"], "CI Tenant A", f"ci-a-{suffix}")
    tenant_b = create_tenant(api, tokens["platform"], "CI Tenant B", f"ci-b-{suffix}")

    provision_initial_admin(api, tokens["platform"], tenant_a, issuer, subjects["tenant-a-admin"])
    provision_initial_admin(api, tokens["platform"], tenant_b, issuer, subjects["tenant-b-admin"])
    set_tenant_active(api, tokens["platform"], tenant_a)
    set_tenant_active(api, tokens["platform"], tenant_b)

    # The same external user has different persisted roles in two tenants.
    membership_a = create_membership(
        api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["shared"], "HospitalAdmin"
    )
    membership_b = create_membership(
        api, tokens["tenant-b-admin"], tenant_b, issuer, subjects["shared"], "FacilityViewer"
    )

    # Provision one real Keycloak identity for each remaining persisted tenant
    # role. These memberships are isolated to the synthetic CI tenant and are
    # used to verify the API's server-derived permission contexts end to end.
    role_memberships = {
        "FacilityViewer": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["facility-viewer"], "FacilityViewer"
        ),
        "FacilityManager": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["facility-manager"], "FacilityManager"
        ),
        "PatientRegistrar": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["patient-registrar"], "PatientRegistrar"
        ),
        "ClinicalUser": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["clinical-user"], "ClinicalUser"
        ),
    }

    hospital_a = create_hospital(
        api, tokens["shared"], tenant_a, "CI Hospital A", f"A-{suffix.upper()}"
    )
    hospital_b = create_hospital(
        api, tokens["tenant-b-admin"], tenant_b, "CI Hospital B", f"B-{suffix.upper()}"
    )

    department_a = create_department(
        api, tokens["shared"], tenant_a, hospital_a,
        "CI Department A", f"DA-{suffix.upper()}"
    )

    department_b = create_department(
        api, tokens["tenant-b-admin"], tenant_b, hospital_b,
        "CI Department B", f"DB-{suffix.upper()}"
    )

    # FacilityViewer is a facility-read-only role. Prove the API rejects
    # patient registration even when called directly with a valid Keycloak token.
    expect_api_status(
        "FacilityViewer cannot register a patient",
        api,
        "POST",
        f"/api/v1/hospitals/{hospital_b}/patients",
        tokens["shared"],
        tenant_b,
        403,
        {
            "givenName": "CI Forbidden",
            "middleName": None,
            "familyName": "FacilityViewer",
            "dateOfBirth": "1990-01-02",
        },
    )

    expect_api_status(
        "FacilityViewer cannot search patients",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients?page=1&pageSize=25",
        tokens["shared"],
        tenant_b,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot read patient identity details",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients/00000000-0000-0000-0000-000000000000",
        tokens["shared"],
        tenant_b,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot search department-scoped patients",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients/departments/{department_b}/patients?page=1&pageSize=25",
        tokens["shared"],
        tenant_b,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot read department-scoped patient identity details",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients/departments/{department_b}/patients/00000000-0000-0000-0000-000000000000",
        tokens["shared"],
        tenant_b,
        403,
    )

    expect_api_status(
        "FacilityViewer cannot run patient duplicate review",
        api,
        "POST",
        f"/api/v1/hospitals/{hospital_b}/patients/duplicate-candidates",
        tokens["shared"],
        tenant_b,
        403,
        {
            "givenName": "CI Forbidden",
            "middleName": None,
            "familyName": "FacilityViewer",
            "dateOfBirth": "1990-01-02",
        },
    )

    contexts = require_status(
        "List current user's active tenant contexts",
        api.call("GET", "/api/v1/me/tenant-contexts?page=1&pageSize=50", tokens["shared"]),
        200,
    )
    items = contexts.get("items", [])
    if contexts.get("totalCount") != 2 or len(items) != 2:
        raise AssertionError(f"Expected two active persisted tenant contexts, received {contexts!r}")
    context_a = next((item for item in items if item.get("tenantId") == tenant_a), None)
    context_b = next((item for item in items if item.get("tenantId") == tenant_b), None)
    if context_a is None or context_b is None:
        raise AssertionError("The two persisted tenant memberships were not both returned.")
    if context_a.get("role") != "HospitalAdmin" or context_b.get("role") != "FacilityViewer":
        raise AssertionError("The same identity did not receive its tenant-specific persisted roles.")
    if "facility.delete" not in context_a.get("permissions", []):
        raise AssertionError("HospitalAdmin permissions were not derived from persisted membership.")
    if "facility.write" in context_b.get("permissions", []) or "facility.delete" in context_b.get("permissions", []):
        raise AssertionError("External or cross-tenant claims escalated the FacilityViewer permissions.")

    # Validate every persisted tenant role using tokens issued by the real
    # Keycloak server. The API must derive the exact expected grants from the
    # persisted membership; token claims are not an authorization source.
    expected_role_permissions = {
        "HospitalAdmin": {"facility.read", "facility.write", "facility.delete", "patient.read", "patient.register", "appointment.manage", "audit.read"},
        "FacilityManager": {"facility.read", "facility.write"},
        "FacilityViewer": {"facility.read"},
        "PatientRegistrar": {"facility.read", "patient.read", "patient.register", "patient.identifier.read", "appointment.manage", "queue.manage", "billing.invoice.manage", "billing.payment.manage"},
        "ClinicalUser": {"facility.read", "patient.identity.read", "queue.read", "encounter.read", "encounter.manage", "encounter.sign", "encounter.amend"},
    }
    role_tokens = {
        "HospitalAdmin": tokens["shared"],
        "FacilityViewer": tokens["facility-viewer"],
        "FacilityManager": tokens["facility-manager"],
        "PatientRegistrar": tokens["patient-registrar"],
        "ClinicalUser": tokens["clinical-user"],
    }
    for role, role_token in role_tokens.items():
        role_contexts = require_status(
            f"Read persisted {role} tenant context",
            api.call("GET", "/api/v1/me/tenant-contexts?page=1&pageSize=50", role_token),
            200,
        )
        role_items = role_contexts.get("items", [])
        context = next((item for item in role_items if item.get("tenantId") == tenant_a), None)
        if context is None or context.get("role") != role:
            raise AssertionError(f"Expected persisted {role} context for tenant A; received {role_contexts!r}")
        permissions = set(context.get("permissions", []))
        missing = expected_role_permissions[role] - permissions
        if missing:
            raise AssertionError(f"{role} is missing expected persisted permissions: {sorted(missing)!r}")
        if role != "HospitalAdmin" and "facility.delete" in permissions:
            raise AssertionError(f"{role} unexpectedly received facility.delete.")
        expect_api_status(
            f"{role} can read the tenant-scoped hospital collection",
            api, "GET", "/api/v1/hospitals", role_token, tenant_a, 200
        )

    # The FacilityManager may maintain facilities but must not read patient
    # records; PatientRegistrar may read patients; ClinicalUser receives only
    # patient identity access for encounter workflows, not general search.
    expect_api_status(
        "FacilityManager cannot search patient records", api, "GET",
        f"/api/v1/hospitals/{hospital_a}/patients?page=1&pageSize=25",
        tokens["facility-manager"], tenant_a, 403
    )
    expect_api_status(
        "PatientRegistrar can search patient records", api, "GET",
        f"/api/v1/hospitals/{hospital_a}/patients?page=1&pageSize=25",
        tokens["patient-registrar"], tenant_a, 200
    )
    expect_api_status(
        "ClinicalUser cannot search patient records", api, "GET",
        f"/api/v1/hospitals/{hospital_a}/patients?page=1&pageSize=25",
        tokens["clinical-user"], tenant_a, 403
    )

    # Exercise duplicate-candidate review with real Keycloak tokens and a
    # synthetic patient. This verifies the persisted tenant role is evaluated
    # by the API, not merely reflected in a UI permission fixture.
    duplicate_family_name = f"Duplicate-{suffix}"
    duplicate_patient = require_status(
        "Register synthetic patient for duplicate-review acceptance",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients",
            tokens["patient-registrar"],
            tenant_a,
            {
                "givenName": "HeapCare",
                "middleName": "Scope",
                "familyName": duplicate_family_name,
                "dateOfBirth": "1990-01-02",
            },
        ),
        201,
    )
    duplicate_patient_id = duplicate_patient.get("id")
    if not duplicate_patient_id:
        raise AssertionError("Synthetic patient registration returned no patient ID.")

    duplicate_review_payload = {
        "givenName": "HeapCare",
        "middleName": "Scope",
        "familyName": duplicate_family_name,
        "dateOfBirth": "1990-01-02",
    }
    duplicate_review_path = f"/api/v1/hospitals/{hospital_a}/patients/duplicate-candidates"
    for role, role_token in (
        ("HospitalAdmin", tokens["shared"]),
        ("PatientRegistrar", tokens["patient-registrar"]),
    ):
        result = require_status(
            f"{role} can review duplicate candidates",
            api.call("POST", duplicate_review_path, role_token, tenant_a, duplicate_review_payload),
            200,
        )
        candidates = result.get("candidates", [])
        if result.get("evaluated") is not True or not any(
            candidate.get("patientId") == duplicate_patient_id for candidate in candidates
        ):
            raise AssertionError(f"{role} did not receive the expected hospital-local duplicate candidate.")

    denied_duplicate_reviews = (
        ("FacilityManager", tokens["facility-manager"], tenant_a, hospital_a),
        ("ClinicalUser", tokens["clinical-user"], tenant_a, hospital_a),
        ("FacilityViewer", tokens["shared"], tenant_b, hospital_b),
    )
    for role, role_token, tenant_id, hospital_id in denied_duplicate_reviews:
        status, body, _ = api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_id}/patients/duplicate-candidates",
            role_token,
            tenant_id,
            duplicate_review_payload,
        )
        if status != 403:
            raise AssertionError(
                f"{role} duplicate review must be forbidden; received HTTP {status}; response={body!r}"
            )
        serialized_body = json.dumps(body)
        if "HeapCare" in serialized_body or duplicate_family_name in serialized_body:
            raise AssertionError(f"{role} duplicate-review denial disclosed submitted patient names.")

    # Reuse the synthetic patient to create an in-progress walk-in.
    # The real ClinicalUser browser journey opens this queue item and retrieves
    # patient identity through the department-scoped clinical API.
    walk_in = require_status(
        "Register synthetic patient as an OPD walk-in",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/queue/walk-ins",
            tokens["patient-registrar"],
            tenant_a,
            {
                "departmentId": department_a,
                "patientId": duplicate_patient_id,
                "timeZoneId": "Asia/Kolkata",
            },
        ),
        201,
    )
    queue_entry_id = walk_in.get("id")
    if not queue_entry_id:
        raise AssertionError("Synthetic walk-in registration returned no queue entry ID.")

    require_status(
        "Call synthetic OPD queue token",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/queue/{queue_entry_id}/call",
            tokens["patient-registrar"],
            tenant_a,
            {"reason": None},
        ),
        200,
    )
    in_progress_queue_entry = require_status(
        "Start synthetic OPD consultation",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/queue/{queue_entry_id}/start",
            tokens["patient-registrar"],
            tenant_a,
            {"reason": None},
        ),
        200,
    )
    if in_progress_queue_entry.get("status") != "InProgress":
        raise AssertionError("Synthetic OPD walk-in did not reach the InProgress state.")

    queue_date = in_progress_queue_entry.get("queueDateLocal")
    if not queue_date:
        raise AssertionError("Synthetic OPD queue entry did not return its hospital-local service date.")
    department_queue_path = (
        f"/api/v1/hospitals/{hospital_a}/queue"
        f"?departmentId={department_a}&date={queue_date}&page=1&pageSize=50"
    )

    registrar_queue = require_status(
        "PatientRegistrar can read the assigned hospital department queue",
        api.call("GET", department_queue_path, tokens["patient-registrar"], tenant_a),
        200,
    )
    registrar_queue_items = registrar_queue.get("items", [])
    if not any(
        item.get("id") == queue_entry_id and item.get("status") == "InProgress"
        for item in registrar_queue_items
    ):
        raise AssertionError("PatientRegistrar did not receive the synthetic in-progress queue entry.")

    clinical_queue = require_status(
        "ClinicalUser can read the assigned hospital department queue",
        api.call("GET", department_queue_path, tokens["clinical-user"], tenant_a),
        200,
    )
    clinical_queue_items = clinical_queue.get("items", [])
    if not any(
        item.get("id") == queue_entry_id and item.get("status") == "InProgress"
        for item in clinical_queue_items
    ):
        raise AssertionError("ClinicalUser did not receive the synthetic in-progress queue entry.")

    expect_api_status(
        "FacilityManager cannot read the OPD department queue",
        api,
        "GET",
        department_queue_path,
        tokens["facility-manager"],
        tenant_a,
        403,
    )

    # Exercise the ClinicalUser encounter lifecycle using a second,
    # isolated synthetic patient/queue entry. Keep the original in-progress
    # encounter untouched for the browser's empty-note/sign-disabled check.
    clinical_patient_family_name = f"Clinical-{suffix.upper()}"
    clinical_patient_payload = {
        "givenName": "CI",
        "middleName": "Synthetic",
        "familyName": clinical_patient_family_name,
        "dateOfBirth": "1991-05-14",
    }
    clinical_duplicate_review = require_status(
        "PatientRegistrar completes duplicate review for the synthetic clinical patient",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients/duplicate-candidates",
            tokens["patient-registrar"],
            tenant_a,
            clinical_patient_payload,
        ),
        200,
    )
    if clinical_duplicate_review.get("evaluated") is not True or clinical_duplicate_review.get("candidates"):
        raise AssertionError("Synthetic clinical patient must pass an explicit no-match duplicate review.")

    clinical_patient = require_status(
        "PatientRegistrar registers the separate synthetic clinical patient",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients",
            tokens["patient-registrar"],
            tenant_a,
            clinical_patient_payload,
        ),
        201,
    )
    clinical_patient_id = clinical_patient.get("id")
    if not clinical_patient_id:
        raise AssertionError("Synthetic clinical patient registration returned no ID.")

    clinical_walk_in = require_status(
        "PatientRegistrar creates a separate synthetic clinical walk-in",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/queue/walk-ins",
            tokens["patient-registrar"],
            tenant_a,
            {
                "departmentId": department_a,
                "patientId": clinical_patient_id,
                "timeZoneId": "Asia/Kolkata",
            },
        ),
        201,
    )
    clinical_queue_entry_id = clinical_walk_in.get("id")
    if not clinical_queue_entry_id:
        raise AssertionError("Synthetic clinical walk-in returned no queue-entry ID.")

    require_status(
        "PatientRegistrar calls the separate synthetic clinical queue entry",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/queue/{clinical_queue_entry_id}/call",
            tokens["patient-registrar"],
            tenant_a,
            {"reason": None},
        ),
        200,
    )
    started_clinical_queue = require_status(
        "PatientRegistrar starts the separate synthetic clinical queue entry",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/queue/{clinical_queue_entry_id}/start",
            tokens["patient-registrar"],
            tenant_a,
            {"reason": None},
        ),
        200,
    )
    if started_clinical_queue.get("status") != "InProgress":
        raise AssertionError("Separate synthetic clinical queue entry did not reach InProgress.")

    clinical_encounter = require_status(
        "ClinicalUser creates an encounter from the in-progress synthetic queue entry",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/opd/queue/{clinical_queue_entry_id}/encounter",
            tokens["clinical-user"],
            tenant_a,
        ),
        201,
    )
    clinical_encounter_id = clinical_encounter.get("id")
    if (
        not clinical_encounter_id
        or clinical_encounter.get("status") != "InProgress"
        or clinical_encounter.get("createdBySubject") != subjects["clinical-user"]
        or clinical_encounter.get("patientId") != clinical_patient_id
    ):
        raise AssertionError("Clinical encounter creation did not preserve its patient, lifecycle or author attribution.")

    duplicate_clinical_encounter_status, _, _ = api.call(
        "POST",
        f"/api/v1/hospitals/{hospital_a}/opd/queue/{clinical_queue_entry_id}/encounter",
        tokens["clinical-user"],
        tenant_a,
    )
    if duplicate_clinical_encounter_status != 409:
        raise AssertionError(
            f"Duplicate encounter creation must return HTTP 409; received HTTP {duplicate_clinical_encounter_status}."
        )

    # Explicit CI placeholders only: no real history, examination, diagnosis
    # or treatment instruction is created in this ephemeral database.
    synthetic_note = {
        "chiefComplaint": "CI synthetic encounter fixture",
        "history": "Automated acceptance placeholder; no real patient history.",
        "examination": "Automated acceptance placeholder; no clinical findings recorded.",
        "assessment": "Synthetic CI assessment placeholder only.",
        "plan": "Synthetic CI plan placeholder only; not a care instruction.",
    }
    encounter_path = f"/api/v1/hospitals/{hospital_a}/opd/encounters/{clinical_encounter_id}"
    drafted_encounter = require_status(
        "ClinicalUser saves a synthetic OPD note draft",
        api.call(
            "PUT",
            encounter_path + "/note",
            tokens["clinical-user"],
            tenant_a,
            synthetic_note,
        ),
        200,
    )
    if drafted_encounter.get("assessment") != synthetic_note["assessment"]:
        raise AssertionError("Synthetic draft assessment was not returned after save.")

    signed_encounter = require_status(
        "ClinicalUser signs the synthetic OPD encounter",
        api.call(
            "POST",
            encounter_path + "/sign",
            tokens["clinical-user"],
            tenant_a,
        ),
        200,
    )
    if (
        signed_encounter.get("status") != "Signed"
        or signed_encounter.get("signedBySubject") != subjects["clinical-user"]
        or not signed_encounter.get("signedAtUtc")
    ):
        raise AssertionError("Signed encounter is missing persisted status or signer attribution.")

    overwrite_status, _, _ = api.call(
        "PUT",
        encounter_path + "/note",
        tokens["clinical-user"],
        tenant_a,
        {**synthetic_note, "assessment": "Changed after signature; must be rejected."},
    )
    if overwrite_status != 409:
        raise AssertionError(
            f"Updating a signed clinical note must return HTTP 409; received HTTP {overwrite_status}."
        )

    require_status(
        "ClinicalUser closes the signed synthetic encounter",
        api.call(
            "POST",
            encounter_path + "/close",
            tokens["clinical-user"],
            tenant_a,
        ),
        200,
    )
    amended_encounter = require_status(
        "ClinicalUser appends an attributed amendment to the closed signed encounter",
        api.call(
            "POST",
            encounter_path + "/amendments",
            tokens["clinical-user"],
            tenant_a,
            {
                "reason": "Synthetic CI amendment",
                "content": "Synthetic amendment fixture only; not a clinical instruction.",
            },
        ),
        200,
    )
    if (
        amended_encounter.get("status") != "Closed"
        or amended_encounter.get("assessment") != synthetic_note["assessment"]
        or len(amended_encounter.get("amendments", [])) != 1
        or amended_encounter["amendments"][0].get("authorSubject") != subjects["clinical-user"]
    ):
        raise AssertionError("Amendment changed the original signed note or lost author attribution.")

    persisted_encounter = require_status(
        "ClinicalUser re-reads the signed encounter and amendment history",
        api.call(
            "GET",
            encounter_path,
            tokens["clinical-user"],
            tenant_a,
        ),
        200,
    )
    if (
        persisted_encounter.get("status") != "Closed"
        or persisted_encounter.get("assessment") != synthetic_note["assessment"]
        or len(persisted_encounter.get("amendments", [])) != 1
    ):
        raise AssertionError("Persisted encounter does not retain the signed original plus one amendment.")

    expect_api_status(
        "PatientRegistrar cannot read the synthetic clinical encounter",
        api,
        "GET",
        encounter_path,
        tokens["patient-registrar"],
        tenant_a,
        403,
    )

    print("Verified real Keycloak ClinicalUser draft, signature attribution, signed-note immutability, close, append-only amendment and PatientRegistrar denial using synthetic CI records.")

    # Verify the OPD billing workflow with real Keycloak-issued HospitalAdmin
    # and PatientRegistrar identities. All visit, invoice and payment records
    # are synthetic and short-lived in this isolated acceptance database.
    service_code = f"CI-OPD-{suffix.upper()}"
    service_item = require_status(
        "HospitalAdmin can create a synthetic OPD billing service item",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/billing/service-items",
            tokens["shared"],
            tenant_a,
            {
                "code": service_code,
                "name": "CI OPD Consultation",
                "category": "Consultation",
                "unitPrice": 500.0,
                "isActive": True,
            },
        ),
        201,
    )
    service_item_id = service_item.get("id")
    if not service_item_id:
        raise AssertionError("Synthetic OPD billing service item returned no ID.")

    invoice_path = f"/api/v1/hospitals/{hospital_a}/billing/invoices"
    invoice = require_status(
        "PatientRegistrar can issue an invoice for the in-progress OPD visit",
        api.call(
            "POST",
            invoice_path,
            tokens["patient-registrar"],
            tenant_a,
            {
                "queueEntryId": queue_entry_id,
                "lines": [{"serviceItemId": service_item_id, "quantity": 1}],
            },
        ),
        201,
    )
    invoice_id = invoice.get("id")
    invoice_number = invoice.get("invoiceNumber")
    if not invoice_id or not invoice_number:
        raise AssertionError("Synthetic OPD invoice did not return its identity and invoice number.")
    if invoice.get("status") != "Issued" or invoice.get("subtotal") != 500.0:
        raise AssertionError(f"Synthetic OPD invoice has an unexpected initial state: {invoice!r}")

    invoice_for_visit = require_status(
        "PatientRegistrar can resume the existing visit invoice",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/billing/visits/{queue_entry_id}/invoice",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if invoice_for_visit.get("id") != invoice_id:
        raise AssertionError("Visit-to-invoice lookup did not return the issued invoice.")

    duplicate_invoice_status, _, _ = api.call(
        "POST",
        invoice_path,
        tokens["patient-registrar"],
        tenant_a,
        {
            "queueEntryId": queue_entry_id,
            "lines": [{"serviceItemId": service_item_id, "quantity": 1}],
        },
    )
    if duplicate_invoice_status != 409:
        raise AssertionError(
            f"Duplicate invoice issue must return HTTP 409; received {duplicate_invoice_status}."
        )

    expect_api_status(
        "FacilityManager cannot issue an OPD invoice",
        api,
        "POST",
        invoice_path,
        tokens["facility-manager"],
        tenant_a,
        403,
        {
            "queueEntryId": queue_entry_id,
            "lines": [{"serviceItemId": service_item_id, "quantity": 1}],
        },
    )

    # Read and payment permissions remain distinct from clinical and
    # facility read access, even within the same hospital tenant.
    expect_api_status(
        "ClinicalUser cannot read the cashier invoice",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_a}/billing/invoices/{invoice_id}",
        tokens["clinical-user"],
        tenant_a,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot read the cashier invoice",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_a}/billing/invoices/{invoice_id}",
        tokens["facility-viewer"],
        tenant_a,
        403,
    )

    payments_path = f"/api/v1/hospitals/{hospital_a}/billing/invoices/{invoice_id}/payments"
    expect_api_status(
        "ClinicalUser cannot record an OPD payment",
        api,
        "POST",
        payments_path,
        tokens["clinical-user"],
        tenant_a,
        403,
        {"amount": 100.0, "method": "Cash", "reference": None},
    )
    partial_payment = require_status(
        "PatientRegistrar can record a partial OPD cash payment",
        api.call(
            "POST",
            payments_path,
            tokens["patient-registrar"],
            tenant_a,
            {"amount": 200.0, "method": "Cash", "reference": None},
        ),
        201,
    )
    partial_invoice = require_status(
        "PatientRegistrar can read the partially settled OPD invoice",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/billing/invoices/{invoice_id}",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if (
        partial_invoice.get("status") != "PartiallyPaid"
        or partial_invoice.get("amountPaid") != 200.0
        or partial_invoice.get("balanceDue") != 300.0
    ):
        raise AssertionError(f"OPD partial settlement has an unexpected state: {partial_invoice!r}")

    payment_reference = f"CI-UPI-{suffix.upper()}"
    final_payment = require_status(
        "PatientRegistrar can settle the remaining OPD invoice balance",
        api.call(
            "POST",
            payments_path,
            tokens["patient-registrar"],
            tenant_a,
            {"amount": 300.0, "method": "UPI", "reference": payment_reference},
        ),
        201,
    )
    final_payment_id = final_payment.get("id")
    if not final_payment_id:
        raise AssertionError("Final OPD payment did not return a payment ID.")

    paid_invoice = require_status(
        "PatientRegistrar can read the fully settled OPD invoice",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/billing/invoices/{invoice_id}",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if (
        paid_invoice.get("status") != "Paid"
        or paid_invoice.get("amountPaid") != 500.0
        or paid_invoice.get("balanceDue") != 0.0
    ):
        raise AssertionError(f"OPD invoice was not fully settled: {paid_invoice!r}")

    payment_history = require_status(
        "PatientRegistrar can read OPD invoice payment history",
        api.call(
            "GET",
            payments_path,
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if (
        not isinstance(payment_history, list)
        or len(payment_history) != 2
        or sum(item.get("amount", 0) for item in payment_history) != 500.0
    ):
        raise AssertionError(f"OPD payment history is incomplete or inconsistent: {payment_history!r}")

    receipt = require_status(
        "PatientRegistrar can retrieve the final OPD payment receipt",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/billing/payments/{final_payment_id}/receipt",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    receipt_number = receipt.get("receiptNumber")
    if (
        not receipt_number
        or receipt.get("invoiceNumber") != invoice_number
        or receipt.get("amount") != 300.0
        or receipt.get("method") != "UPI"
        or receipt.get("reference") != payment_reference
    ):
        raise AssertionError(f"OPD payment receipt is inconsistent with the payment ledger: {receipt!r}")

    daily_collection = require_status(
        "PatientRegistrar can read the synthetic OPD daily collection",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/billing/reports/daily-collection"
            f"?businessDate={queue_date}&timeZoneId=Asia%2FKolkata",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if (
        daily_collection.get("totalCollected") != 500.0
        or daily_collection.get("paymentCount") != 2
        or daily_collection.get("byMethod", {}).get("Cash") != 200.0
        or daily_collection.get("byMethod", {}).get("UPI") != 300.0
    ):
        raise AssertionError(f"OPD daily collection does not match the synthetic payment ledger: {daily_collection!r}")

    # Publish one synthetic provider slot using the real staff and scheduling
    # endpoints. Leave the slot unbooked so the PatientRegistrar browser journey
    # performs the actual booking and appointment check-in.
    appointment_zone = ZoneInfo("Asia/Kolkata")
    appointment_start = (
        datetime.now(appointment_zone) + timedelta(minutes=45)
    ).replace(second=0, microsecond=0)
    appointment_start += timedelta(minutes=(-appointment_start.minute) % 10)
    appointment_end = appointment_start + timedelta(minutes=10)
    appointment_date = appointment_start.date().isoformat()

    provider_profile = require_status(
        "HospitalAdmin can create a synthetic assigned OPD provider profile",
        api.call(
            "POST",
            "/api/v1/staff",
            tokens["shared"],
            tenant_a,
            {
                "staffCode": f"CI-APPT-{suffix.upper()}",
                "givenName": "HeapCare",
                "middleName": "CI",
                "familyName": "Consultant",
                "providerDesignation": "Consultant",
                "hospitalId": hospital_a,
                "departmentId": department_a,
                "effectiveFrom": appointment_date,
            },
        ),
        201,
    )
    provider_staff_id = provider_profile.get("id")
    if not provider_staff_id:
        raise AssertionError("Synthetic provider profile did not return a staff ID.")
    provider_assignments = provider_profile.get("assignments", [])
    if not any(
        assignment.get("hospitalId") == hospital_a
        and assignment.get("departmentId") == department_a
        and assignment.get("isActive") is True
        for assignment in provider_assignments
    ):
        raise AssertionError("Synthetic provider profile did not receive its active department assignment.")

    expect_api_status(
        "PatientRegistrar cannot create a provider staff profile",
        api,
        "POST",
        "/api/v1/staff",
        tokens["patient-registrar"],
        tenant_a,
        403,
        {
            "staffCode": f"CI-APPT-DENIED-{suffix.upper()}",
            "givenName": "Forbidden",
            "middleName": None,
            "familyName": "Provider",
            "providerDesignation": "Consultant",
            "hospitalId": hospital_a,
            "departmentId": department_a,
            "effectiveFrom": appointment_date,
        },
    )

    availability_path = f"/api/v1/hospitals/{hospital_a}/availability-sessions"
    availability_payload = {
        "departmentId": department_a,
        "providerStaffId": provider_staff_id,
        "localDate": appointment_date,
        "startLocalTime": appointment_start.time().isoformat(timespec="seconds"),
        "endLocalTime": appointment_end.time().isoformat(timespec="seconds"),
        "slotDurationMinutes": 10,
        "timeZoneId": "Asia/Kolkata",
    }
    expect_api_status(
        "FacilityManager cannot publish provider availability",
        api,
        "POST",
        availability_path,
        tokens["facility-manager"],
        tenant_a,
        403,
        availability_payload,
    )
    provider_session = require_status(
        "HospitalAdmin can publish the synthetic provider availability session",
        api.call(
            "POST",
            availability_path,
            tokens["shared"],
            tenant_a,
            availability_payload,
        ),
        201,
    )
    appointment_session_id = provider_session.get("id")
    if not appointment_session_id:
        raise AssertionError("Synthetic availability session did not return its ID.")

    availability_query = (
        f"?date={appointment_date}&departmentId={department_a}&page=1&pageSize=25"
    )
    registrar_availability = require_status(
        "PatientRegistrar can discover published provider availability",
        api.call(
            "GET",
            availability_path + availability_query,
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if not any(
        item.get("id") == appointment_session_id
        for item in registrar_availability.get("items", [])
    ):
        raise AssertionError("PatientRegistrar did not receive the synthetic provider availability session.")

    expect_api_status(
        "ClinicalUser cannot read provider availability",
        api,
        "GET",
        availability_path + availability_query,
        tokens["clinical-user"],
        tenant_a,
        403,
    )
    provider_slots = require_status(
        "PatientRegistrar can retrieve the published provider slots",
        api.call(
            "GET",
            f"{availability_path}/{appointment_session_id}/slots",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    available_provider_slots = [
        item for item in provider_slots if item.get("isAvailable") is True
    ]
    if len(available_provider_slots) != 1:
        raise AssertionError(
            f"Expected exactly one available synthetic appointment slot; received {provider_slots!r}"
        )
    appointment_slot_starts_at_utc = available_provider_slots[0].get("startsAtUtc")
    if not appointment_slot_starts_at_utc:
        raise AssertionError("Synthetic appointment slot did not return its UTC start instant.")

    appointment_patient_family_name = f"Appointment-{suffix}"
    appointment_patient_payload = {
        "givenName": "Browser",
        "middleName": "Appointment",
        "familyName": appointment_patient_family_name,
        "dateOfBirth": "1992-06-15",
    }
    appointment_duplicate_review = require_status(
        "PatientRegistrar can complete duplicate review before appointment registration",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients/duplicate-candidates",
            tokens["patient-registrar"],
            tenant_a,
            appointment_patient_payload,
        ),
        200,
    )
    if appointment_duplicate_review.get("evaluated") is not True or appointment_duplicate_review.get("candidates"):
        raise AssertionError(
            "The isolated appointment patient must have a completed no-match duplicate review."
        )
    appointment_patient = require_status(
        "PatientRegistrar can register a dedicated synthetic appointment patient",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients",
            tokens["patient-registrar"],
            tenant_a,
            appointment_patient_payload,
        ),
        201,
    )
    appointment_patient_id = appointment_patient.get("id")
    if not appointment_patient_id:
        raise AssertionError("Synthetic appointment patient registration returned no patient ID.")

    # Cancellation lifecycle acceptance uses a separate appointment on the
    # already-published browser slot. It is cancelled before the browser books
    # that slot, so the real UI journey can still consume the same availability.
    cancellation_api_appointment = require_status(
        "PatientRegistrar can book a synthetic appointment for cancellation acceptance",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/appointments",
            tokens["patient-registrar"],
            tenant_a,
            {
                "availabilitySessionId": appointment_session_id,
                "patientId": appointment_patient_id,
                "slotStartsAtUtc": appointment_slot_starts_at_utc,
                "reason": "Synthetic cancellation lifecycle acceptance",
            },
        ),
        201,
    )
    cancellation_api_appointment_id = cancellation_api_appointment.get("id")
    if not cancellation_api_appointment_id:
        raise AssertionError("Synthetic cancellation appointment returned no ID.")

    cancelled_api_appointment = require_status(
        "PatientRegistrar can cancel a booked appointment",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/appointments/{cancellation_api_appointment_id}/cancel",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if cancelled_api_appointment.get("status") != "Cancelled":
        raise AssertionError(f"Appointment cancellation did not persist Cancelled status: {cancelled_api_appointment!r}")

    repeat_cancel_status, _, _ = api.call(
        "POST",
        f"/api/v1/hospitals/{hospital_a}/appointments/{cancellation_api_appointment_id}/cancel",
        tokens["patient-registrar"],
        tenant_a,
    )
    if repeat_cancel_status != 409:
        raise AssertionError(f"Repeated appointment cancellation must return HTTP 409; received {repeat_cancel_status}.")

    cancelled_check_in_status, _, _ = api.call(
        "POST",
        f"/api/v1/hospitals/{hospital_a}/appointments/{cancellation_api_appointment_id}/check-in",
        tokens["patient-registrar"],
        tenant_a,
    )
    if cancelled_check_in_status != 409:
        raise AssertionError(f"Cancelled appointment check-in must return HTTP 409; received {cancelled_check_in_status}.")

    cancelled_reschedule_status, _, _ = api.call(
        "POST",
        f"/api/v1/hospitals/{hospital_a}/appointments/{cancellation_api_appointment_id}/reschedule",
        tokens["patient-registrar"],
        tenant_a,
        {
            "availabilitySessionId": appointment_session_id,
            "patientId": appointment_patient_id,
            "slotStartsAtUtc": appointment_slot_starts_at_utc,
            "reason": "Must not revive cancelled appointment",
        },
    )
    if cancelled_reschedule_status != 409:
        raise AssertionError(f"Cancelled appointment reschedule must return HTTP 409; received {cancelled_reschedule_status}.")

    cancelled_api_readback = require_status(
        "PatientRegistrar can read the retained cancelled appointment",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/appointments/{cancellation_api_appointment_id}",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if cancelled_api_readback.get("status") != "Cancelled":
        raise AssertionError(f"Cancelled appointment was unexpectedly revived: {cancelled_api_readback!r}")

    # Publish a second non-overlapping session for one UI-cancel appointment
    # and one already-checked-in appointment. The former is left Booked for
    # the real browser workspace; the latter proves a 409 cannot orphan its
    # durable queue entry.
    cancellation_session_start = appointment_start + timedelta(minutes=15)
    cancellation_session_end = cancellation_session_start + timedelta(minutes=15)
    cancellation_session_payload = {
        "departmentId": department_a,
        "providerStaffId": provider_staff_id,
        "localDate": appointment_date,
        "startLocalTime": cancellation_session_start.time().isoformat(timespec="seconds"),
        "endLocalTime": cancellation_session_end.time().isoformat(timespec="seconds"),
        "slotDurationMinutes": 5,
        "timeZoneId": "Asia/Kolkata",
    }
    cancellation_session = require_status(
        "HospitalAdmin can publish separate appointment cancellation acceptance slots",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/availability-sessions",
            tokens["shared"],
            tenant_a,
            cancellation_session_payload,
        ),
        201,
    )
    cancellation_session_id = cancellation_session.get("id")
    if not cancellation_session_id:
        raise AssertionError("Cancellation acceptance availability session returned no ID.")

    cancellation_slots = require_status(
        "PatientRegistrar can read cancellation acceptance slots",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/availability-sessions/{cancellation_session_id}/slots",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    available_cancellation_slots = [slot for slot in cancellation_slots if slot.get("isAvailable") is True]
    if len(available_cancellation_slots) != 3:
        raise AssertionError(
            f"Expected three available synthetic cancellation slots; received {cancellation_slots!r}"
        )

    cancellation_ui_family_name = f"ManagementCancel-{suffix}"
    cancellation_ui_patient_payload = {
        "givenName": "Browser",
        "middleName": "Management",
        "familyName": cancellation_ui_family_name,
        "dateOfBirth": "1991-04-23",
    }
    cancellation_ui_duplicate_review = require_status(
        "PatientRegistrar can complete duplicate review for the appointment-management synthetic patient",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients/duplicate-candidates",
            tokens["patient-registrar"],
            tenant_a,
            cancellation_ui_patient_payload,
        ),
        200,
    )
    if (
        cancellation_ui_duplicate_review.get("evaluated") is not True
        or cancellation_ui_duplicate_review.get("candidates")
    ):
        raise AssertionError("Appointment-management synthetic patient must have a completed no-match duplicate review.")

    cancellation_ui_patient = require_status(
        "PatientRegistrar can register a dedicated appointment-management patient",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/patients",
            tokens["patient-registrar"],
            tenant_a,
            cancellation_ui_patient_payload,
        ),
        201,
    )
    cancellation_ui_patient_id = cancellation_ui_patient.get("id")
    if not cancellation_ui_patient_id:
        raise AssertionError("Appointment-management synthetic patient returned no ID.")

    cancellation_ui_appointment = require_status(
        "PatientRegistrar can book the separate appointment reserved for browser cancellation",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/appointments",
            tokens["patient-registrar"],
            tenant_a,
            {
                "availabilitySessionId": cancellation_session_id,
                "patientId": cancellation_ui_patient_id,
                "slotStartsAtUtc": available_cancellation_slots[0].get("startsAtUtc"),
                "reason": "Synthetic browser cancellation acceptance",
            },
        ),
        201,
    )
    cancellation_ui_appointment_id = cancellation_ui_appointment.get("id")
    if not cancellation_ui_appointment_id:
        raise AssertionError("Browser cancellation appointment returned no ID.")
    if cancellation_ui_appointment.get("status") != "Booked":
        raise AssertionError(f"Browser cancellation appointment was not Booked: {cancellation_ui_appointment!r}")

    checked_in_guard_appointment = require_status(
        "PatientRegistrar can book a synthetic appointment for cancellation-after-check-in acceptance",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/appointments",
            tokens["patient-registrar"],
            tenant_a,
            {
                "availabilitySessionId": cancellation_session_id,
                "patientId": appointment_patient_id,
                "slotStartsAtUtc": available_cancellation_slots[1].get("startsAtUtc"),
                "reason": "Synthetic cancellation after check-in acceptance",
            },
        ),
        201,
    )
    checked_in_guard_appointment_id = checked_in_guard_appointment.get("id")
    if not checked_in_guard_appointment_id:
        raise AssertionError("Checked-in cancellation guard appointment returned no ID.")

    checked_in_guard_queue = require_status(
        "PatientRegistrar can check in the appointment reserved for cancellation guard acceptance",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_a}/appointments/{checked_in_guard_appointment_id}/check-in",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    checked_in_guard_queue_id = checked_in_guard_queue.get("id")
    if not checked_in_guard_queue_id:
        raise AssertionError("Checked-in cancellation guard did not return a queue-entry ID.")

    checked_in_cancel_status, _, _ = api.call(
        "POST",
        f"/api/v1/hospitals/{hospital_a}/appointments/{checked_in_guard_appointment_id}/cancel",
        tokens["patient-registrar"],
        tenant_a,
    )
    if checked_in_cancel_status != 409:
        raise AssertionError(
            f"Cancelling an appointment after check-in must return HTTP 409; received {checked_in_cancel_status}."
        )

    checked_in_guard_readback = require_status(
        "PatientRegistrar can read the appointment retained after checked-in cancellation denial",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/appointments/{checked_in_guard_appointment_id}",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if checked_in_guard_readback.get("status") != "Booked":
        raise AssertionError(
            f"Checked-in appointment should remain Booked after cancellation denial: {checked_in_guard_readback!r}"
        )

    queue_date = checked_in_guard_queue.get("queueDateLocal")
    checked_in_queue_page = require_status(
        "PatientRegistrar can verify the checked-in appointment queue entry remains present",
        api.call(
            "GET",
            f"/api/v1/hospitals/{hospital_a}/queue"
            f"?departmentId={department_a}&date={queue_date}&page=1&pageSize=50",
            tokens["patient-registrar"],
            tenant_a,
        ),
        200,
    )
    if not any(item.get("id") == checked_in_guard_queue_id for item in checked_in_queue_page.get("items", [])):
        raise AssertionError("Cancellation denial removed or hid the checked-in appointment queue entry.")

    # Tenant data and permissions must follow the selected persisted membership.
    list_a = require_status(
        "List hospitals in tenant A",
        api.call("GET", "/api/v1/hospitals", tokens["shared"], tenant_a),
        200,
    )
    list_b = require_status(
        "List hospitals in tenant B",
        api.call("GET", "/api/v1/hospitals", tokens["shared"], tenant_b),
        200,
    )
    ids_a = {item["id"] for item in list_a.get("items", [])}
    ids_b = {item["id"] for item in list_b.get("items", [])}
    if ids_a != {hospital_a} or ids_b != {hospital_b}:
        raise AssertionError(
            f"Tenant data isolation failed: tenant A IDs={ids_a!r}; tenant B IDs={ids_b!r}"
        )

    expect_api_status(
        "Tenant A identity cannot read tenant B hospital",
        api, "GET", f"/api/v1/hospitals/{hospital_b}", tokens["shared"], tenant_a, 404
    )
    expect_api_status(
        "Tenant B identity cannot read tenant A hospital",
        api, "GET", f"/api/v1/hospitals/{hospital_a}", tokens["shared"], tenant_b, 404
    )
    expect_api_status(
        "FacilityViewer cannot create a hospital",
        api, "POST", "/api/v1/hospitals", tokens["shared"], tenant_b, 403
    )
    expect_api_status(
        "Platform operator without tenant membership cannot select tenant A",
        api, "GET", "/api/v1/hospitals", tokens["platform"], tenant_a, 403
    )
    expect_api_status(
        "Authenticated request without tenant selection is rejected",
        api, "GET", "/api/v1/hospitals", tokens["shared"], None, 400
    )

    # A tenant administrator cannot mutate a membership owned by another tenant.
    expect_api_status(
        "Tenant A administrator cannot update tenant B membership",
        api, "PUT", f"/api/v1/tenant/memberships/{membership_b}",
        tokens["tenant-a-admin"], tenant_a, 404,
        {"role": "FacilityViewer", "scopes": [{"scopeType": "Tenant"}]}
    )
    expect_api_status(
        "Tenant B administrator cannot update tenant A membership",
        api, "PUT", f"/api/v1/tenant/memberships/{membership_a}",
        tokens["tenant-b-admin"], tenant_b, 404,
        {"role": "FacilityViewer", "scopes": [{"scopeType": "Tenant"}]}
    )

    # Revocation takes effect on the next request without invalidating access to
    # the user's independent membership in tenant A.
    require_status(
        "Deactivate tenant B membership",
        api.call(
            "POST",
            f"/api/v1/tenant/memberships/{membership_b}/deactivate",
            tokens["tenant-b-admin"],
            tenant_b,
        ),
        200,
    )
    expect_api_status(
        "Deactivated tenant B membership is denied",
        api, "GET", "/api/v1/hospitals", tokens["shared"], tenant_b, 403
    )
    expect_api_status(
        "Tenant A membership remains active after tenant B revocation",
        api, "GET", "/api/v1/hospitals", tokens["shared"], tenant_a, 200
    )
    inactive_contexts = require_status(
        "List contexts after tenant B revocation",
        api.call("GET", "/api/v1/me/tenant-contexts?page=1&pageSize=50", tokens["shared"]),
        200,
    )
    remaining = inactive_contexts.get("items", [])
    if inactive_contexts.get("totalCount") != 1 or len(remaining) != 1 or remaining[0].get("tenantId") != tenant_a:
        raise AssertionError(f"Inactive tenant membership remained visible: {inactive_contexts!r}")

    require_status(
        "Reactivate tenant B membership",
        api.call(
            "POST",
            f"/api/v1/tenant/memberships/{membership_b}/reactivate",
            tokens["tenant-b-admin"],
            tenant_b,
        ),
        200,
    )
    expect_api_status(
        "Reactivated tenant B membership regains its own tenant access",
        api, "GET", "/api/v1/hospitals", tokens["shared"], tenant_b, 200
    )

    # Hand the same persisted two-tenant identity to the real browser PKCE
    # test through a runner-temporary file; never write credentials to the repo
    # or emit them into the Actions log.
    browser_user_file = os.environ.get("HEAPCARE_BROWSER_TEST_USER_FILE")
    if not browser_user_file:
        raise RuntimeError("HEAPCARE_BROWSER_TEST_USER_FILE must point to the runner-temporary browser credential file.")
    browser_user_path = Path(browser_user_file)
    browser_user_path.parent.mkdir(parents=True, exist_ok=True)
    browser_user_path.write_text(
        json.dumps(
            {
                # Preserve the original shared identity for the full patient
                # registration and cross-tenant browser journey.
                "username": usernames["shared"],
                "password": password,
                # Separate synthetic identities allow real OIDC browser tests
                # to exercise the actual persisted role for each workspace.
                "users": {
                    name: {"username": username, "password": password}
                    for name, username in usernames.items()
                },
                "tenantAId": tenant_a,
                "tenantBId": tenant_b,
                "hospitalAId": hospital_a,
                "hospitalBId": hospital_b,
                "departmentAId": department_a,
                "queueEntryId": queue_entry_id,
                "queueTokenNumber": in_progress_queue_entry.get("tokenNumber"),
                "queueDateLocal": in_progress_queue_entry.get("queueDateLocal"),
                "patientFamilyName": duplicate_family_name,
                "billingInvoiceNumber": invoice_number,
                "billingReceiptNumber": receipt_number,
                "appointmentDateLocal": appointment_date,
                "appointmentSessionId": appointment_session_id,
                "appointmentSlotStartsAtUtc": appointment_slot_starts_at_utc,
                "appointmentPatientId": appointment_patient_id,
                "appointmentPatientFamilyName": appointment_patient_family_name,
                "appointmentManagementCancelPatientId": cancellation_ui_patient_id,
                "appointmentManagementCancelAppointmentId": cancellation_ui_appointment_id,
                "appointmentManagementCancelPatientFamilyName": cancellation_ui_family_name,
                "clinicalQueueEntryId": clinical_queue_entry_id,
                "clinicalQueueTokenNumber": started_clinical_queue.get("tokenNumber"),
                "clinicalQueueDateLocal": started_clinical_queue.get("queueDateLocal"),
                "clinicalPatientFamilyName": clinical_patient_family_name,
            }
        ),
        encoding="utf-8",
    )
    browser_user_path.chmod(0o600)

    browser_manifest = json.loads(browser_user_path.read_text(encoding="utf-8"))
    required_browser_roles = {
        "platform",
        "shared",
        "facility-viewer",
        "facility-manager",
        "patient-registrar",
        "clinical-user",
    }
    configured_browser_roles = browser_manifest.get("users", {})
    if not required_browser_roles.issubset(configured_browser_roles):
        raise AssertionError(
            "The ephemeral browser credential manifest is missing one or more persisted-role identities."
        )
    for role_name in required_browser_roles:
        role_credentials = configured_browser_roles[role_name]
        if not role_credentials.get("username") or not role_credentials.get("password"):
            raise AssertionError(f"The ephemeral browser identity for {role_name} is incomplete.")
    if not all(
        browser_manifest.get(name)
        for name in ("tenantAId", "tenantBId", "hospitalAId", "hospitalBId", "departmentAId")
    ):
        raise AssertionError("The ephemeral browser manifest is missing its synthetic tenant or organization IDs.")

    required_opd_fixture_fields = (
        "queueEntryId",
        "queueTokenNumber",
        "queueDateLocal",
        "patientFamilyName",
        "billingInvoiceNumber",
        "billingReceiptNumber",
        "appointmentDateLocal",
        "appointmentSessionId",
        "appointmentSlotStartsAtUtc",
        "appointmentPatientId",
        "appointmentPatientFamilyName",
        "appointmentManagementCancelPatientId",
        "appointmentManagementCancelAppointmentId",
        "appointmentManagementCancelPatientFamilyName",
        "clinicalQueueEntryId",
        "clinicalQueueTokenNumber",
        "clinicalQueueDateLocal",
        "clinicalPatientFamilyName",
    )
    if not all(browser_manifest.get(name) for name in required_opd_fixture_fields):
        raise AssertionError("The ephemeral browser manifest is missing its synthetic in-progress OPD queue fixture.")

    print("Prepared all persisted-role OIDC identities, a synthetic in-progress OPD visit, and a settled billing invoice/receipt for real browser acceptance.")

    print("Verified real Keycloak-issued tokens against HeapCare.Api issuer/audience validation.")
    print("Verified real Keycloak-issued identities for all five persisted tenant roles and server-derived role permissions.")
    print("Verified hospital data isolation, cross-tenant membership mutation denial, and membership revocation/reactivation.")
    print("Verified FacilityViewer and ClinicalUser/FacilityManager patient-search denials and PatientRegistrar patient-search access.")
    print("No real patient or billing data was created.")


def main() -> None:
    run()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not print request headers, credentials, refresh/access tokens or secrets.
        print(f"AUTHENTICATED TENANCY ACCEPTANCE FAILED: {error}", file=__import__("sys").stderr)
        raise SystemExit(1)
