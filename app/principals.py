from dataclasses import dataclass
from typing import Literal, get_args


ApiKeyScope = Literal[
    "events:write",
    "endpoints:read",
    "endpoints:write",
    "deliveries:read",
    "deliveries:replay",
]
API_KEY_SCOPES = frozenset(get_args(ApiKeyScope))


@dataclass(frozen=True)
class MachinePrincipal:
    credential_id: str
    project_id: int
    scopes: frozenset[str]
