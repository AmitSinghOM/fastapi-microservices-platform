"""Synchronous producer and management clients with safe failures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import httpx

EnvelopeMode = Literal["native", "cloudevents"]
QueryValue = str | int | float | bool | None

# Sentinel distinguishing "argument not passed" from an explicit None,
# which the PATCH endpoint treats as "clear this field".
_UNSET: Any = object()


class WebhookPlatformError(Exception):
    """Base class that never embeds credentials or response bodies."""


class TransportError(WebhookPlatformError):
    """The service could not be reached within the configured timeout."""

    def __init__(self) -> None:
        super().__init__("Webhook Platform request failed before a response")


class ApiError(WebhookPlatformError):
    """A bounded HTTP failure without raw response content."""

    def __init__(
        self,
        status_code: int,
        code: str = "HTTP_ERROR",
        retry_after: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        super().__init__(
            f"Webhook Platform request failed "
            f"(status={status_code}, code={code})"
        )


def _safe_error(response: httpx.Response) -> ApiError:
    code = "HTTP_ERROR"
    try:
        document = response.json()
        candidate = document.get("error", {}).get("code")
        if isinstance(candidate, str) and candidate.isascii():
            if 1 <= len(candidate) <= 64:
                code = candidate
    except (ValueError, AttributeError):
        pass
    retry_after: int | None = None
    raw_retry_after = response.headers.get("Retry-After")
    if raw_retry_after and raw_retry_after.isdigit():
        retry_after = min(int(raw_retry_after), 86_400)
    return ApiError(response.status_code, code, retry_after)


class _JsonClient:
    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        timeout: httpx.Timeout | float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._headers = dict(headers)
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout
            or httpx.Timeout(10.0, connect=5.0, write=10.0, pool=5.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, QueryValue] | None = None,
    ) -> Any:
        try:
            request_headers = self._headers | dict(headers or {})
            response = self._client.request(
                method,
                path,
                json=json,
                data=data,
                headers=request_headers,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise TransportError() from exc
        if response.status_code not in {200, 201, 202, 204}:
            raise _safe_error(response)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(response.status_code, "INVALID_RESPONSE") from exc


class AuthClient(_JsonClient):
    """Register users and exchange passwords without retaining credentials."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            base_url,
            {"User-Agent": "webhook-platform-python/0.1"},
            timeout=timeout,
            client=client,
        )

    def register(
        self, email: str, name: str, password: str
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/users/",
            json={"email": email, "name": name, "password": password},
        )
        if not isinstance(result, dict):
            raise ApiError(201, "INVALID_RESPONSE")
        return result

    def login(self, email: str, password: str) -> str:
        result = self._request(
            "POST",
            "/auth/login",
            data={"username": email, "password": password},
        )
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise ApiError(200, "INVALID_RESPONSE")
        return token


class Producer(_JsonClient):
    """Send events without implicit retries.

    A caller may retry only with the same idempotency key. The client never
    retries automatically, so an ambiguous transport failure cannot create a
    second logical event under a different key.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        super().__init__(
            base_url,
            {
                "X-API-Key": api_key,
                "User-Agent": "webhook-platform-python/0.1",
            },
            timeout=timeout,
            client=client,
        )

    def send_event(
        self,
        event_type: str,
        payload: object,
        idempotency_key: str,
        *,
        envelope_mode: EnvelopeMode = "native",
    ) -> dict[str, Any]:
        if not event_type or not idempotency_key:
            raise ValueError("event_type and idempotency_key are required")
        result = self._request(
            "POST",
            "/v1/events",
            json={
                "type": event_type,
                "payload": payload,
                "envelope_mode": envelope_mode,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        if not isinstance(result, dict):
            raise ApiError(202, "INVALID_RESPONSE")
        return result


class ManagementClient(_JsonClient):
    """Bearer-authenticated control-plane operations used by the CLI."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError("token is required")
        super().__init__(
            base_url,
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": "webhook-platform-python/0.1",
            },
            timeout=timeout,
            client=client,
        )

    def list_organizations(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/organizations")

    def create_organization(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/v1/organizations", json={"name": name})

    def list_projects(self, organization_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/v1/organizations/{organization_id}/projects"
        )

    def create_project(
        self, organization_id: str, name: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/organizations/{organization_id}/projects",
            json={"name": name},
        )

    def list_api_keys(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/api-keys")

    def create_api_key(
        self,
        project_id: str,
        name: str,
        *,
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/api-keys",
            json={
                "name": name,
                "scopes": ["events:write"],
                "expires_in_days": expires_in_days,
            },
        )

    def revoke_api_key(
        self, project_id: str, api_key_id: str
    ) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/v1/projects/{project_id}/api-keys/{api_key_id}"
        )

    def list_endpoints(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/endpoints")

    def create_endpoint(
        self,
        project_id: str,
        url: str,
        description: str | None = None,
        *,
        event_types: list[str] | None = None,
        signature_scheme: str | None = None,
    ) -> dict[str, Any]:
        """Create an endpoint, optionally subscribed to specific types.

        ``event_types`` accepts exact event types and trailing ``prefix.*``
        wildcards. ``signature_scheme`` selects ``legacy`` or ``standard``
        wire signatures (ADR 0002). Optional fields are left off the
        request when unset so the call also works against servers that
        predate them.
        """
        body: dict[str, Any] = {"url": url, "description": description}
        if event_types is not None:
            body["event_types"] = list(event_types)
        if signature_scheme is not None:
            body["signature_scheme"] = signature_scheme
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/endpoints",
            json=body,
        )

    def update_endpoint(
        self,
        project_id: str,
        endpoint_id: str,
        *,
        url: str = _UNSET,
        description: str | None = _UNSET,
        is_active: bool = _UNSET,
        event_types: list[str] | None = _UNSET,
        signature_scheme: str = _UNSET,
    ) -> dict[str, Any]:
        """Partially update an endpoint.

        Only keyword arguments that are passed are sent, matching the API's
        PATCH semantics. Pass ``event_types=None`` explicitly to clear a
        subscription filter. Changing ``signature_scheme`` to ``standard``
        returns a one-time ``signing_secret`` usable with any Standard
        Webhooks library; capture it directly into a secret manager.
        """
        changes: dict[str, Any] = {}
        for field, value in (
            ("url", url),
            ("description", description),
            ("is_active", is_active),
            ("event_types", event_types),
            ("signature_scheme", signature_scheme),
        ):
            if value is not _UNSET:
                changes[field] = value
        if not changes:
            raise ValueError("update_endpoint requires at least one change")
        return self._request(
            "PATCH",
            f"/v1/projects/{project_id}/endpoints/{endpoint_id}",
            json=changes,
        )

    def list_deliveries(
        self, project_id: str, *, offset: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/v1/projects/{project_id}/deliveries",
            params={"offset": offset, "limit": limit},
        )

    def get_delivery(
        self, project_id: str, delivery_id: str
    ) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/projects/{project_id}/deliveries/{delivery_id}"
        )

    def replay_delivery(
        self, project_id: str, delivery_id: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/deliveries/{delivery_id}/replay",
        )

    def replay_deliveries(
        self,
        project_id: str,
        delivery_ids: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/replays",
            json={"delivery_ids": delivery_ids},
            headers={"Idempotency-Key": idempotency_key},
        )
