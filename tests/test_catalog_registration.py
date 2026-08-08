"""Catalog registration must not duplicate routes when accounts race.

`_register_hub_catalog` runs from a BackgroundTask, one per GitHub account.
Adding three accounts together on dev-16 produced 108 model_routes rows for 36
distinct model names — three bursts of exactly 36, 26s and 1.1s apart. The
function read the existing catalog at the top, then spent tens of seconds in
`ensure_pooled_provider_api`'s ARM calls before inserting, so all three copies
read an empty catalog and all three inserted the full list.

The consequences are not cosmetic: the portal reports 108 models, the smoke
test runs 216 calls instead of 72, and a lookup by model name is ambiguous.

Hermetic: in-memory SQLite, ARM and the hub's catalog API stubbed. The database
backstop (a partial unique index) is a Postgres-only DDL statement in
`app/init_db.py`, so it is not exercised here — these tests cover the
application-level ordering, and `test_concurrent_insert_is_not_a_deploy_failure`
covers what happens when the index does fire.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api import github_accounts as ga
from app.models.enums import OwnerScope
from app.models.orm import Base, ModelRoute

FQDN = "h-test.example.azurecontainerapps.io"
CATALOG = ["gpt-4o-mini", "claude-opus-4.8", "gemini-3.5-flash"]


@pytest.fixture()
def db() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def stub_hub(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the hub catalog fetch. Returns the call-order log for ARM vs read."""
    order: list[str] = []
    monkeypatch.setattr(ga, "_fetch_hub_models", lambda _f, _t: list(CATALOG))

    class _Prov:
        def ensure_pooled_provider_api(self, provider: str) -> str:
            order.append(f"arm:{provider}")
            return f"llm-{provider}-pool"

    monkeypatch.setattr(ga, "ApimProvisioner", _Prov)
    return order


def _names(db: Any) -> list[str]:
    return sorted(r.name for r in db.query(ModelRoute).all())


def test_first_registration_creates_one_route_per_model(db: Any, stub_hub: list[str]) -> None:
    ga._register_hub_catalog(db, FQDN, "tok")
    assert _names(db) == sorted(CATALOG)


def test_second_account_adds_no_duplicates(db: Any, stub_hub: list[str]) -> None:
    """The documented contract: later accounts only add pool members."""
    ga._register_hub_catalog(db, FQDN, "tok")
    ga._register_hub_catalog(db, "h-second.example.com", "tok")
    assert _names(db) == sorted(CATALOG)
    assert len(db.query(ModelRoute).all()) == len(CATALOG)


def test_arm_calls_happen_before_the_catalog_is_read(
    db: Any, stub_hub: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual fix, stated as an ordering property.

    Every second the ARM calls spend between the read and the insert is a second
    in which a concurrent registration can read a catalog that is about to be
    stale. Moving them ahead of the read does not close the window — only the
    unique index does that — but it shrinks it from tens of seconds to the
    duration of a query plus an insert.
    """
    order = stub_hub
    real_query = db.query

    def _tracking_query(model: Any):  # noqa: ANN202 - test shim
        if model is ModelRoute:
            order.append("read")
        return real_query(model)

    monkeypatch.setattr(db, "query", _tracking_query)
    ga._register_hub_catalog(db, FQDN, "tok")

    assert "read" in order, "the catalog was never read"
    first_read = order.index("read")
    arm_calls = [i for i, ev in enumerate(order) if ev.startswith("arm:")]
    assert arm_calls, "no provider API was wired"
    assert max(arm_calls) < first_read, (
        f"ARM work still happens inside the read-to-insert window: {order}"
    )


def test_concurrent_insert_is_not_a_deploy_failure(
    db: Any, stub_hub: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the unique index fires, the row we wanted already exists.

    Losing that race is the desired end state, so it must not propagate: this
    runs inside `_deploy_account`, and raising here would mark a perfectly good
    account FAILED over a duplicate we did not need to create.
    """
    def _boom() -> None:
        raise IntegrityError("duplicate key", None, Exception("unique violation"))

    monkeypatch.setattr(db, "commit", _boom)
    rolled_back: list[bool] = []
    monkeypatch.setattr(db, "rollback", lambda: rolled_back.append(True))

    ga._register_hub_catalog(db, FQDN, "tok")  # must not raise

    assert rolled_back == [True], "a failed commit must be rolled back"


def test_platform_routes_are_what_gets_deduped(db: Any, stub_hub: list[str]) -> None:
    """A tenant's BYO route may reuse a platform model name — the dedupe (and
    the partial index behind it) must be scoped to PLATFORM, or adding an
    account would collide with a customer's own route."""
    db.add(
        ModelRoute(
            id="rt_tenantbyo01",
            tenant_id="tn_customer",
            name="gpt-4o-mini",
            provider=ga.Provider("openai"),
            apim_backend_or_pool_id="byo-backend",
            owner_scope=OwnerScope.TENANT,
            auth_mode=ga.AuthMode("KV_SECRET"),
        )
    )
    db.commit()

    ga._register_hub_catalog(db, FQDN, "tok")

    rows = db.query(ModelRoute).filter(ModelRoute.name == "gpt-4o-mini").all()
    scopes = sorted(r.owner_scope for r in rows)
    assert scopes == [OwnerScope.PLATFORM, OwnerScope.TENANT], (
        "the tenant's BYO route and the platform route must coexist"
    )
