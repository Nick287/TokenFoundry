"""Model route router: self-service "add a model" (admin).

This is the FastAPI-driven model onboarding the user chose over the portal
wizard. It registers an APIM backend (+ optional header-auth secret in KV) and
records the alias->backend mapping. BYO routes carry a tenant_id and store the
tenant's own provider key, isolated in Key Vault.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import get_db
from app.models.orm import ModelRoute
from app.models.schemas import ModelRouteCreate, ModelRouteOut, ModelRouteUpdate
from app.services.apim_provisioner import ApimProvisioner
from app.services.keyvault import KeyVaultService

router = APIRouter()


@router.post(
    "/routes", response_model=ModelRouteOut, status_code=status.HTTP_201_CREATED
)
def add_route(
    body: ModelRouteCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> ModelRoute:
    route_id = f"rt_{uuid.uuid4().hex[:12]}"

    provisioner = ApimProvisioner()
    backend_id: str | None = None
    if body.backend_url and body.backend_secret:
        # Store the upstream key in Key Vault (per-route reference) and wire the
        # provider's client-facing API + shared backend in APIM. Each provider
        # (openai/anthropic/google) gets its own API with a native subscription
        # header + its own backend; adding a model of that provider just ensures
        # the provider API exists and points at the shared backend.
        kv = KeyVaultService()
        kv.set_secret(KeyVaultService.backend_secret_name(route_id), body.backend_secret)
        backend_id = provisioner.ensure_provider_api(
            body.provider.value, body.backend_url, body.backend_secret
        )

    route = ModelRoute(
        id=route_id,
        tenant_id=body.tenant_id,
        name=body.name,
        provider=body.provider,
        apim_backend_or_pool_id=backend_id,
        deployment_name=body.deployment_name,
        api_version=body.api_version,
        owner_scope=body.owner_scope,
        auth_mode=body.auth_mode,
        price_in_per_1k=body.price_in_per_1k,
        price_out_per_1k=body.price_out_per_1k,
        markup_pct=body.markup_pct,
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return route


@router.get("/routes", response_model=list[ModelRouteOut])
def list_routes(
    db: Session = Depends(get_db), _: Principal = Depends(require_admin)
) -> list[ModelRoute]:
    return list(db.query(ModelRoute).all())


@router.patch("/routes/{route_id}", response_model=ModelRouteOut)
def update_route(
    route_id: str,
    body: ModelRouteUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> ModelRoute:
    """Edit a route's display fields: alias, deployment/version and
    pricing/markup. Provider, backend and KV secret are immutable here — delete
    and re-add to change those."""
    route = db.get(ModelRoute, route_id)
    if not route:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="route not found")
    if body.name is not None:
        route.name = body.name
    if body.deployment_name is not None:
        route.deployment_name = body.deployment_name
    if body.api_version is not None:
        route.api_version = body.api_version
    if body.price_in_per_1k is not None:
        route.price_in_per_1k = body.price_in_per_1k
    if body.price_out_per_1k is not None:
        route.price_out_per_1k = body.price_out_per_1k
    if body.markup_pct is not None:
        route.markup_pct = body.markup_pct
    db.commit()
    db.refresh(route)
    return route


@router.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(
    route_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    """Remove a model route. Deletes the DB record and best-effort cleans up the
    per-route Key Vault secret. The shared provider backend/API are left intact
    (other models of the same provider still use them)."""
    route = db.get(ModelRoute, route_id)
    if not route:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="route not found")
    try:
        KeyVaultService().delete_secret(KeyVaultService.backend_secret_name(route_id))
    except Exception:  # noqa: BLE001 — secret may not exist; don't block deletion
        logging.getLogger(__name__).info("route %s secret cleanup skipped", route_id)
    db.delete(route)
    db.commit()
