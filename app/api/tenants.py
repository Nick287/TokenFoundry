"""Admin routers: tenants and projects (platform-operator only)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import get_db
from app.models.orm import Project, Tenant, VirtualKey
from app.models.schemas import (
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    TenantCreate,
    TenantOut,
    TenantUpdate,
)
from app.services.apim_provisioner import ApimProvisioner

logger = logging.getLogger(__name__)

router = APIRouter()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: TenantCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Tenant:
    # Bind an APIM product so virtual keys can be issued for this tenant.
    # Tolerate APIM being unavailable: the tenant is still created (product
    # can be attached later) rather than failing the whole operation.
    tenant_id = _new_id("tn")
    product_ids: list[str] = []
    try:
        product_id = ApimProvisioner().ensure_product_for_tenant(tenant_id)
        product_ids = [product_id]
    except Exception:  # noqa: BLE001 — never block tenant creation on APIM
        logger.exception("APIM product binding failed; creating tenant without one")

    tenant = Tenant(
        id=tenant_id,
        name=body.name,
        mode=body.mode,
        billing_account_id=body.billing_account_id,
        apim_product_ids=product_ids,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(
    db: Session = Depends(get_db), _: Principal = Depends(require_admin)
) -> list[Tenant]:
    return list(db.query(Tenant).all())


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found"
        )
    if body.name is not None:
        tenant.name = body.name
    if body.mode is not None:
        tenant.mode = body.mode
    db.commit()
    db.refresh(tenant)
    return tenant


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    """Delete a tenant and cascade its projects + keys. Refuses while live
    (active/suspended) keys exist so a delete never silently revokes traffic."""
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    live = (
        db.query(VirtualKey)
        .join(Project, VirtualKey.project_id == Project.id)
        .filter(Project.tenant_id == tenant_id)
        .count()
    )
    if live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"tenant has {live} key(s); delete those first",
        )
    db.delete(tenant)
    db.commit()


@router.post("/tenants/{tenant_id}/ensure-product", response_model=TenantOut)
def ensure_tenant_product(
    tenant_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Tenant:
    """Backfill an APIM product for a tenant created before auto-binding existed
    (or whose binding failed). Idempotent: no-op if already bound."""
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found"
        )
    if not tenant.apim_product_ids:
        product_id = ApimProvisioner().ensure_product_for_tenant(tenant_id)
        tenant.apim_product_ids = [product_id]
        db.commit()
        db.refresh(tenant)
    return tenant


@router.post(
    "/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED
)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Project:
    if not db.get(Tenant, body.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found"
        )
    project = Project(
        id=_new_id("pj"),
        tenant_id=body.tenant_id,
        name=body.name,
        cost_center=body.cost_center,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(
    tenant_id: str | None = None,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> list[Project]:
    q = db.query(Project)
    if tenant_id:
        q = q.filter(Project.tenant_id == tenant_id)
    return list(q.all())


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    body: ProjectUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if body.name is not None:
        project.name = body.name
    if body.cost_center is not None:
        project.cost_center = body.cost_center
    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    """Delete a project. Refuses while it still has keys (delete those first)."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    live = db.query(VirtualKey).filter(VirtualKey.project_id == project_id).count()
    if live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"project has {live} key(s); delete those first",
        )
    db.delete(project)
    db.commit()
