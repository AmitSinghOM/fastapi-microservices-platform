import csv
from io import StringIO

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api_key_auth import get_api_key_project
from app.auth import get_current_active_user
from app.dependencies import get_webhook_service
from app.models import Project, User
from app.schemas.webhooks import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    DeliveryCancelRequest,
    DeliveryDetail,
    DeliveryOut,
    DeliveryPurgeOut,
    DeliveryPurgeRequest,
    EndpointCreate,
    EndpointCreated,
    EndpointOut,
    EndpointPauseRequest,
    EndpointRuntimeOut,
    EndpointSecretRotated,
    EndpointUpdate,
    EventCreate,
    EventOut,
    MemberCreate,
    MemberOut,
    OrganizationCreate,
    OrganizationOut,
    ProjectCreate,
    ProjectOut,
    ReplayBatchRequest,
    ReplayOperationOut,
    ReplayOut,
)
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/v1", tags=["webhooks"])


def page(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> tuple[int, int]:
    return offset, limit


@router.post(
    "/organizations",
    response_model=OrganizationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    body: OrganizationCreate,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.create_organization(user.id, body.name)


@router.get("/organizations", response_model=list[OrganizationOut])
async def list_organizations(
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_organizations(user.id, *pagination)


@router.post(
    "/organizations/{organization_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    organization_id: str,
    body: MemberCreate,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.add_member(
        user.id, organization_id, body.user_id, body.role
    )


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[MemberOut],
)
async def list_members(
    organization_id: str,
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_members(user.id, organization_id, *pagination)


@router.post(
    "/organizations/{organization_id}/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    organization_id: str,
    body: ProjectCreate,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.create_project(user.id, organization_id, body.name)


@router.get(
    "/organizations/{organization_id}/projects",
    response_model=list[ProjectOut],
)
async def list_projects(
    organization_id: str,
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_projects(user.id, organization_id, *pagination)


@router.delete("/projects/{project_id}", response_model=ProjectOut)
async def deactivate_project(
    project_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.deactivate_project(user.id, project_id)


@router.post(
    "/projects/{project_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    project_id: str,
    body: ApiKeyCreate,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    api_key, plaintext = await service.create_api_key(
        user.id, project_id, body.name
    )
    data = ApiKeyOut.model_validate(api_key).model_dump()
    return ApiKeyCreated(**data, plaintext_key=plaintext)


@router.get(
    "/projects/{project_id}/api-keys", response_model=list[ApiKeyOut]
)
async def list_api_keys(
    project_id: str,
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_api_keys(user.id, project_id, *pagination)


@router.delete(
    "/projects/{project_id}/api-keys/{key_id}", response_model=ApiKeyOut
)
async def revoke_api_key(
    project_id: str,
    key_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.revoke_api_key(user.id, project_id, key_id)


@router.post(
    "/projects/{project_id}/endpoints",
    response_model=EndpointCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(
    project_id: str,
    body: EndpointCreate,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    endpoint, secret = await service.create_endpoint(
        user.id, project_id, str(body.url), body.description
    )
    data = EndpointOut.model_validate(endpoint).model_dump()
    return EndpointCreated(**data, signing_secret=secret)


@router.get(
    "/projects/{project_id}/endpoints", response_model=list[EndpointOut]
)
async def list_endpoints(
    project_id: str,
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_endpoints(user.id, project_id, *pagination)


@router.patch(
    "/projects/{project_id}/endpoints/{endpoint_id}",
    response_model=EndpointOut,
)
async def update_endpoint(
    project_id: str,
    endpoint_id: str,
    body: EndpointUpdate,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    changes = body.model_dump(exclude_unset=True)
    if changes.get("url") is not None:
        changes["url"] = str(changes["url"])
    return await service.update_endpoint(
        user.id, project_id, endpoint_id, changes
    )


@router.post(
    "/projects/{project_id}/endpoints/{endpoint_id}/rotate-secret",
    response_model=EndpointSecretRotated,
)
async def rotate_endpoint_secret(
    project_id: str,
    endpoint_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    endpoint, secret = await service.rotate_endpoint_secret(
        user.id, project_id, endpoint_id
    )
    return EndpointSecretRotated(
        public_id=endpoint.public_id,
        secret_version=endpoint.secret_version,
        signing_secret=secret,
    )


@router.post(
    "/events", response_model=EventOut, status_code=status.HTTP_202_ACCEPTED
)
async def ingest_event(
    body: EventCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    project: Project = Depends(get_api_key_project),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.ingest_event(
        project, idempotency_key, body.type, body.payload
    )


@router.get("/projects/{project_id}/events", response_model=list[EventOut])
async def list_events(
    project_id: str,
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_events(user.id, project_id, *pagination)


@router.get(
    "/projects/{project_id}/events/{event_id}", response_model=EventOut
)
async def get_event(
    project_id: str,
    event_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.get_event(user.id, project_id, event_id)


@router.get(
    "/projects/{project_id}/deliveries", response_model=list[DeliveryOut]
)
async def list_deliveries(
    project_id: str,
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_deliveries(user.id, project_id, *pagination)


@router.get(
    "/projects/{project_id}/deliveries/{delivery_id}",
    response_model=DeliveryDetail,
)
async def get_delivery(
    project_id: str,
    delivery_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.get_delivery(user.id, project_id, delivery_id)


@router.post(
    "/projects/{project_id}/deliveries/{delivery_id}/replay",
    response_model=ReplayOut,
    status_code=status.HTTP_201_CREATED,
)
async def replay_delivery(
    project_id: str,
    delivery_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.replay_delivery(user.id, project_id, delivery_id)


@router.post(
    "/projects/{project_id}/replays",
    response_model=ReplayOperationOut,
    status_code=status.HTTP_201_CREATED,
)
async def replay_deliveries(
    project_id: str,
    body: ReplayBatchRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.replay_deliveries(
        user.id, project_id, body.delivery_ids, idempotency_key
    )


@router.get(
    "/projects/{project_id}/dead-deliveries",
    response_model=list[DeliveryOut],
)
async def list_dead_deliveries(
    project_id: str,
    endpoint_id: str | None = Query(default=None),
    reason: str | None = Query(default=None, max_length=64),
    minimum_age_seconds: int | None = Query(default=None, ge=0),
    pagination: tuple[int, int] = Depends(page),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.list_dead_deliveries(
        user.id,
        project_id,
        *pagination,
        endpoint_id=endpoint_id,
        reason=reason,
        minimum_age_seconds=minimum_age_seconds,
    )


@router.get("/projects/{project_id}/dead-deliveries/export")
async def export_dead_deliveries(
    project_id: str,
    endpoint_id: str | None = Query(default=None),
    reason: str | None = Query(default=None, max_length=64),
    minimum_age_seconds: int | None = Query(default=None, ge=0),
    limit: int = Query(default=1_000, ge=1, le=1_000),
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    deliveries = await service.list_dead_deliveries(
        user.id,
        project_id,
        0,
        limit,
        endpoint_id=endpoint_id,
        reason=reason,
        minimum_age_seconds=minimum_age_seconds,
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        (
            "delivery_id",
            "endpoint_id",
            "dead_reason",
            "attempt_count",
            "last_http_status",
            "dead_at",
        )
    )
    for delivery in deliveries:
        writer.writerow(
            (
                delivery.public_id,
                delivery.endpoint_public_id_snapshot,
                delivery.dead_reason,
                delivery.attempt_count,
                delivery.last_http_status,
                delivery.dead_at.isoformat() if delivery.dead_at else "",
            )
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="dead-deliveries-{project_id}.csv"'
            )
        },
    )


@router.post(
    "/projects/{project_id}/endpoints/{endpoint_id}/pause",
    response_model=EndpointRuntimeOut,
)
async def pause_endpoint(
    project_id: str,
    endpoint_id: str,
    body: EndpointPauseRequest,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.pause_endpoint(
        user.id, project_id, endpoint_id, body.reason
    )


@router.post(
    "/projects/{project_id}/endpoints/{endpoint_id}/resume",
    response_model=EndpointRuntimeOut,
)
async def resume_endpoint(
    project_id: str,
    endpoint_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.resume_endpoint(user.id, project_id, endpoint_id)


@router.post(
    "/projects/{project_id}/endpoints/{endpoint_id}/recover-circuit",
    response_model=EndpointRuntimeOut,
)
async def recover_endpoint_circuit(
    project_id: str,
    endpoint_id: str,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.recover_endpoint_circuit(
        user.id, project_id, endpoint_id
    )


@router.post(
    "/projects/{project_id}/deliveries/{delivery_id}/cancel",
    response_model=DeliveryOut,
)
async def cancel_delivery(
    project_id: str,
    delivery_id: str,
    body: DeliveryCancelRequest,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.cancel_delivery(
        user.id, project_id, delivery_id, body.reason
    )


@router.post(
    "/projects/{project_id}/deliveries/purge",
    response_model=DeliveryPurgeOut,
)
async def purge_terminal_deliveries(
    project_id: str,
    body: DeliveryPurgeRequest,
    user: User = Depends(get_current_active_user),
    service: WebhookService = Depends(get_webhook_service),
):
    return await service.purge_terminal_deliveries(
        user.id,
        project_id,
        body.dry_run,
        body.max_records,
    )
