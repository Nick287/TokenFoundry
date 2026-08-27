"""Breakdown group labels — the name that rides alongside an opaque id.

Two of the five breakdown dimensions group on an id that is durable but
unreadable. `backend` is a hub id (`gha_5fe0a527b3bc`); `subscription` is a
virtual-key id (`vk_9c8b9aff08dc`). Neither tells an operator whose spend they
are looking at, which is the whole reason to group by them.

The id stays the identity — a GitHub login can be renamed, a project can be
renamed, the id cannot — and a human-readable name comes along as `label`. The
portal already renders that label for every id-shaped dimension, so labelling a
new one is a server-side change only.

`backend` was labelled first; `subscription` was not, even though the call log
had been resolving key -> project all along and the hub labeller's own docstring
pointed at it as the precedent. These tests pin both halves, and the one rule
they share: when nothing resolves, NO label is attached. A key or an account
deleted after its calls were billed still has rows in Cosmos. That row is real
spend with no owner on record any more, and inventing a name for it would be
worse than showing the raw id.

Hermetic: in-memory SQLite, no Azure, no Cosmos.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import usage as u
from app.models.enums import TenantMode
from app.models.orm import Base, GitHubAccount, Project, Tenant, VirtualKey

KEY_A, KEY_B = "vk_aaaa1111", "vk_bbbb2222"
HUB = "gha_cccc3333"


@pytest.fixture()
def db() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="t1", name="Acme", mode=TenantMode.INTERNAL))
    session.add(Project(id="p1", tenant_id="t1", name="搜索团队"))
    session.add(Project(id="p2", tenant_id="t1", name="推荐团队"))
    session.add(VirtualKey(id=KEY_A, project_id="p1"))
    session.add(VirtualKey(id=KEY_B, project_id="p2"))
    session.add(GitHubAccount(id=HUB, github_login="acme-bot"))
    session.commit()
    yield session
    session.close()


def _payload(by: str, *ids: str) -> dict[str, Any]:
    """A breakdown payload shaped the way cost_breakdown returns it: the group id
    lives under the DIMENSION NAME, not under a generic key."""
    return {"by": by, "groups": [{by: i, "calls": 1} for i in ids]}


# --- subscription: the half that was missing ---------------------------------


def test_key_groups_get_their_project_name(db: Any) -> None:
    out = u._label_groups(_payload("subscription", KEY_A, KEY_B), db)
    assert [g["label"] for g in out["groups"]] == ["搜索团队", "推荐团队"]


def test_the_key_id_is_not_replaced_by_the_name(db: Any) -> None:
    """The id is the identity and the join key. A project can be renamed; losing
    the id would make the row impossible to trace back to a key."""
    out = u._label_groups(_payload("subscription", KEY_A), db)
    assert out["groups"][0]["subscription"] == KEY_A


def test_a_key_with_no_project_on_record_gets_no_label(db: Any) -> None:
    """Deleted key, historical spend. Cosmos still has the rows; PostgreSQL does
    not have the ownership. A blank label says "unknown owner" — a made-up one
    would say something false about who to bill."""
    out = u._label_groups(_payload("subscription", "vk_deleted"), db)
    assert "label" not in out["groups"][0]


def test_only_the_keys_in_the_payload_are_queried(db: Any, monkeypatch) -> None:
    """The platform-wide breakdown has no tenant to filter on, so scoping by the
    ids in hand is what stops it pulling every key in the system to label a
    handful of groups."""
    seen: list[list[str]] = []
    real = u._project_names_for
    monkeypatch.setattr(u, "_project_names_for",
                        lambda d, ids: (seen.append(list(ids)), real(d, ids))[1])
    monkeypatch.setitem(u._LABELLERS, "subscription", u._project_names_for)
    u._label_groups(_payload("subscription", KEY_A), db)
    assert seen == [[KEY_A]]


def test_no_groups_means_no_query(db: Any) -> None:
    """An empty window is common (a fresh key, a quiet hour) and must not cost a
    round trip that can only return nothing."""
    assert u._project_names_for(db, []) == {}


# --- backend: the half that already worked, kept working ----------------------


def test_hub_groups_still_get_their_github_login(db: Any) -> None:
    out = u._label_groups(_payload("backend", HUB), db)
    assert out["groups"][0]["label"] == "acme-bot"


def test_an_unknown_hub_gets_no_label(db: Any) -> None:
    out = u._label_groups(_payload("backend", "gha_gone"), db)
    assert "label" not in out["groups"][0]


# --- the dimensions that read as themselves ----------------------------------


@pytest.mark.parametrize("by", ["model", "api", "end_user"])
def test_self_describing_dimensions_are_left_alone(db: Any, by: str) -> None:
    """`claude-opus-4.8` and `llm-anthropic` need no translation, and an end_user
    is whatever the customer sent — we have nothing to resolve it against."""
    out = u._label_groups(_payload(by, "x"), db)
    assert "label" not in out["groups"][0]


def test_an_unrecognised_dimension_does_not_raise(db: Any) -> None:
    """`by` reaches this from a query parameter. A value the labeller does not
    know must pass through, not 500 the breakdown."""
    out = u._label_groups({"by": "nonsense", "groups": [{"nonsense": "x"}]}, db)
    assert out["groups"][0] == {"nonsense": "x"}


def test_groups_missing_their_dimension_key_are_skipped(db: Any) -> None:
    """Defensive: a group with no id cannot be labelled, and must not take the
    whole breakdown down with it."""
    out = u._label_groups({"by": "subscription", "groups": [{"calls": 1}]}, db)
    assert "label" not in out["groups"][0]
