#!/usr/bin/env python3
"""Idempotently provision the CompileCare Logs Kibana data view for local use."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["kbn-xsrf"] = "compilecare-local-provisioner"

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            result = {"message": raw.decode("utf-8", errors="replace")}
        return error.code, result


def provision(base_url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            status, _ = request(base_url, "/api/status")
            if status == 200:
                break
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(2)
    else:
        raise RuntimeError(f"Kibana did not become ready at {base_url}: {last_error}")

    status, listing = request(base_url, "/api/data_views")
    if status != 200:
        raise RuntimeError(f"Kibana data view listing failed with HTTP {status}: {listing}")

    existing = next(
        (
            view
            for view in listing.get("data_view", [])
            if view.get("title") == "compilecare-logs-*"
        ),
        None,
    )
    if existing:
        print(
            "CompileCare Logs data view already exists "
            f"(id={existing.get('id')}, title={existing.get('title')})."
        )
        return

    payload = {
        "data_view": {
            "name": "CompileCare Logs",
            "title": "compilecare-logs-*",
            "timeFieldName": "@timestamp",
            "allowNoIndex": True,
        }
    }
    status, created = request(
        base_url,
        "/api/data_views/data_view",
        method="POST",
        payload=payload,
    )
    if status not in (200, 201):
        raise RuntimeError(
            f"Kibana could not create the CompileCare Logs data view "
            f"(HTTP {status}): {created}"
        )

    data_view = created.get("data_view", {})
    if data_view.get("title") != "compilecare-logs-*":
        raise RuntimeError(f"Kibana returned an unexpected data view: {created}")

    print(
        "Provisioned CompileCare Logs data view "
        f"(id={data_view.get('id')}, title={data_view.get('title')}, "
        f"timeField={data_view.get('timeFieldName')})."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:5601",
        help="Kibana base URL (default: http://localhost:5601)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Maximum wait for Kibana readiness (default: 180)",
    )
    args = parser.parse_args()

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    provision(args.base_url, args.timeout_seconds)


if __name__ == "__main__":
    main()
