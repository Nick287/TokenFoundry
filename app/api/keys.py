"""Virtual key router: provision an APIM subscription + store its key in KV.

The raw subscription key is returned EXACTLY ONCE (at creation) and otherwise
only ever referenced via Key Vault — never persisted in PostgreSQL.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import get_db
from app.models.enums import KeyStatus
from app.models.orm import Project, Tenant, VirtualKey
from app.models.schemas import VirtualKeyCreate, VirtualKeyOut, VirtualKeySecret
from app.services.apim_provisioner import ApimProvisioner
from app.services.keyvault import KeyVaultService

router = APIRouter()


@router.get("/keys", response_model=list[VirtualKeyOut])
def list_keys(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> list[VirtualKey]:
    """List issued virtual keys (metadata only — the secret VALUE never returns
    here; it lives in Key Vault and is shown exactly once at creation)."""
    q = db.query(VirtualKey)
    if project_id:
        q = q.filter(VirtualKey.project_id == project_id)
    return list(q.order_by(VirtualKey.created_at.desc()).all())


@router.post(
    "/keys", response_model=VirtualKeySecret, status_code=status.HTTP_201_CREATED
)
def create_key(
    body: VirtualKeyCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> VirtualKeySecret:
    project = db.get(Project, body.project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    tenant = db.get(Tenant, project.tenant_id)
    if not tenant or not tenant.apim_product_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="tenant has no APIM product provisioned",
        )

    key_id = f"vk_{uuid.uuid4().hex[:12]}"
    product_id = tenant.apim_product_ids[0]

    provisioner = ApimProvisioner()
    kv = KeyVaultService()

    # 1) create the APIM subscription -> returns the primary key value
    key_value = provisioner.create_subscription(
        subscription_id=key_id,
        display_name=f"{tenant.name}/{project.name}/{key_id}",
        product_id=product_id,
    )
    # 2) store the value in Key Vault; persist only the reference
    kv_ref = kv.set_secret(KeyVaultService.subscription_key_name(key_id), key_value)

    vk = VirtualKey(
        id=key_id,
        project_id=project.id,
        apim_subscription_id=key_id,
        keyvault_ref=kv_ref,
        allowed_route_ids=body.allowed_route_ids,
        tpm_tier=body.tpm_tier,
        monthly_budget_usd=body.monthly_budget_usd,
        budget_action=body.budget_action,
        expires_at=body.expires_at,
        status=KeyStatus.ACTIVE,
    )
    db.add(vk)
    db.commit()
    db.refresh(vk)

    out = VirtualKeyOut.model_validate(vk).model_dump()
    return VirtualKeySecret(**out, key_value=key_value)


@router.post("/keys/{key_id}/suspend", response_model=VirtualKeyOut)
def suspend_key(
    key_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> VirtualKey:
    vk = db.get(VirtualKey, key_id)
    if not vk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
    if vk.apim_subscription_id:
        ApimProvisioner().set_subscription_state(vk.apim_subscription_id, "suspended")
    vk.status = KeyStatus.SUSPENDED
    db.commit()
    db.refresh(vk)
    return vk


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_key(
    key_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    """Permanently revoke a virtual key: cancel its APIM subscription, drop the
    Key Vault secret, and delete the row. Best-effort on the cloud cleanup so a
    stale Azure object never blocks removing the record."""
    vk = db.get(VirtualKey, key_id)
    if not vk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
    if vk.apim_subscription_id:
        try:
            ApimProvisioner().delete_subscription(vk.apim_subscription_id)
        except Exception:  # noqa: BLE001 — never block deletion on APIM
            pass
    try:
        KeyVaultService().delete_secret(KeyVaultService.subscription_key_name(key_id))
    except Exception:  # noqa: BLE001 — secret may already be gone
        pass
    db.delete(vk)
    db.commit()
