"""Tenant-safe routing helpers for provider-owned phone numbers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .clients import list_client_ids, load_client
from .constants import CLIENTS_DIR


class TenantRoutingError(RuntimeError):
    """Base error for tenant routing validation failures."""


class UnknownPiopiyNumberError(TenantRoutingError):
    """Raised when an inbound Piopiy DID is not assigned to any client."""


class DuplicatePiopiyNumberError(TenantRoutingError):
    """Raised when a Piopiy DID is assigned to more than one route."""


@dataclass(frozen=True, slots=True)
class PiopiyNumberRoute:
    did: str
    client_id: str
    project_id: str | None
    project_name: str | None = None
    agent_id: str | None = None
    app_id: str | None = None


def normalize_phone_digits(value: object) -> str:
    """Normalize a phone number to digits only for provider DID matching."""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _candidate_dids(*values: object) -> set[str]:
    dids: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            normalized = normalize_phone_digits(value)
            if normalized:
                dids.add(normalized)
            continue
        if isinstance(value, Iterable):
            for item in value:
                normalized = normalize_phone_digits(item)
                if normalized:
                    dids.add(normalized)
    return dids


def build_piopiy_number_registry(base_dir: Path = CLIENTS_DIR) -> dict[str, PiopiyNumberRoute]:
    """Build a unique DID -> client/project registry from client project config.

    The registry intentionally ignores global fallback environment variables.
    Inbound calls must resolve through an explicit client-owned number to avoid
    cross-tenant contamination.
    """
    registry: dict[str, PiopiyNumberRoute] = {}
    owners: dict[str, list[PiopiyNumberRoute]] = {}
    for client_id in list_client_ids(base_dir=base_dir):
        try:
            bundle = load_client(client_id, base_dir=base_dir)
        except Exception:
            continue
        for project in bundle.projects:
            runtime = project.runtime
            for did in _candidate_dids(runtime.piopiy_dids):
                route = PiopiyNumberRoute(
                    did=did,
                    client_id=client_id,
                    project_id=project.project_id,
                    project_name=project.name,
                    agent_id=runtime.piopiy_agent_id,
                    app_id=runtime.piopiy_app_id,
                )
                owners.setdefault(did, []).append(route)
    duplicate_dids = {
        did: routes
        for did, routes in owners.items()
        if len({(route.client_id, route.project_id) for route in routes}) > 1
    }
    if duplicate_dids:
        details = "; ".join(
            f"{did}: "
            + ", ".join(f"{route.client_id}/{route.project_id or '-'}" for route in routes)
            for did, routes in sorted(duplicate_dids.items())
        )
        raise DuplicatePiopiyNumberError(f"Duplicate Piopiy DID ownership detected: {details}")
    for did, routes in owners.items():
        if routes:
            registry[did] = routes[0]
    return registry


def resolve_piopiy_number_route(
    did: object,
    *,
    base_dir: Path = CLIENTS_DIR,
) -> PiopiyNumberRoute:
    """Resolve a Piopiy DID to exactly one client/project route."""
    normalized = normalize_phone_digits(did)
    if not normalized:
        raise UnknownPiopiyNumberError("Missing Piopiy DID in inbound payload.")
    registry = build_piopiy_number_registry(base_dir=base_dir)
    candidate_keys = [
        normalized,
        normalized[-12:] if len(normalized) >= 12 else "",
        normalized[-10:] if len(normalized) >= 10 else "",
    ]
    for key in candidate_keys:
        if key and key in registry:
            return registry[key]
    raise UnknownPiopiyNumberError(f"Piopiy DID {normalized} is not assigned to any client.")


def assert_piopiy_number_owned_by_client(
    *,
    did: object,
    client_id: str,
    project_id: str | None = None,
    base_dir: Path = CLIENTS_DIR,
) -> PiopiyNumberRoute:
    """Validate outbound/runtime use of a Piopiy number before a call starts."""
    route = resolve_piopiy_number_route(did, base_dir=base_dir)
    requested_client = str(client_id or "").strip()
    requested_project = str(project_id or "").strip() or None
    if route.client_id != requested_client:
        raise TenantRoutingError(
            f"Piopiy DID {route.did} belongs to {route.client_id}, not {requested_client}."
        )
    return route
