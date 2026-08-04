"""Control-plane re-login: identity guard and Key Vault write ordering.

`relogin_poll` replaces a live hub's Copilot token. Two of its properties are
security/correctness properties rather than features, and both fail quietly:

1. **The token must belong to the same GitHub account.** The browser that
   authorizes the device code may be signed into someone else's GitHub. Install
   that token and the hub starts spending a different person's Copilot quota
   while the portal, the APIM pool membership and every usage record still name
   the original login — a mis-billing with no error anywhere.

2. **Key Vault is written only after the hub accepted the token.** KV is the
   durable copy that the next deploy injects. If it were written first, a token
   the hub rejected would still be picked up by the next redeploy, turning a
   failed button click into a scheduled outage.

Hermetic: in-memory SQLite for the account row, and every outbound call (GitHub,
Key Vault, the hub's admin API) stubbed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import github_accounts as ga
from app.models.enums import DeployStatus
from app.models.orm import Base, GitHubAccount

ACCOUNT_ID = "gha_test0001"
FQDN = "h-test.example.azurecontainerapps.io"


@pytest.fixture()
def db() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        GitHubAccount(
            id=ACCOUNT_ID,
            github_login="alice",
            github_user_id="12345",
            oauth_token_kv_ref="github-token-old",
            admin_token_kv_ref="hub-admin-token",
            status=DeployStatus.READY,
            device_code="dev_code_123",
            container_app_fqdn=FQDN,
            backend_ids=[],
        )
    )
    session.commit()
    yield session
    session.close()


class _FakeKeyVault:
    """Records writes so ordering against the hub call can be asserted."""

    def __init__(self, journal: list[tuple[str, str]]) -> None:
        self._journal = journal

    def get_secret(self, name: str) -> str | None:
        return "admin-token-value" if name == "hub-admin-token" else None

    def set_secret(self, name: str, value: str) -> None:
        self._journal.append(("kv", name))


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    poll: dict[str, Any],
    who: dict[str, Any],
    install_error: Exception | None = None,
) -> list[tuple[str, str]]:
    """Stub every outbound call; return the ordered journal of side effects."""
    journal: list[tuple[str, str]] = []

    monkeypatch.setattr(ga.copilot_device, "poll_device_flow", lambda _code: poll)
    monkeypatch.setattr(ga.copilot_device, "whoami", lambda _tok: who)
    monkeypatch.setattr(ga, "KeyVaultService", lambda: _FakeKeyVault(journal))

    def _install(fqdn: str, admin_token: str, oauth_token: str) -> None:
        journal.append(("hub", oauth_token))
        if install_error:
            raise install_error

    monkeypatch.setattr(ga, "_install_token_on_hub", _install)
    return journal


_SUCCESS = {"status": "success", "access_token": "ghu_fresh"}


def test_happy_path_installs_then_persists(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = _wire(monkeypatch, poll=_SUCCESS, who={"login": "alice", "id": "12345"})

    out = ga.relogin_poll(ACCOUNT_ID, db=db)

    assert out.status == "success"
    assert out.github_login == "alice"
    # Hub FIRST, Key Vault SECOND — KV must never hold an unproven token.
    assert journal == [("hub", "ghu_fresh"), ("kv", ga._github_token_name(ACCOUNT_ID))]

    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    # Re-login deploys nothing, so the deploy state machine must not move.
    assert acct.status is DeployStatus.READY
    assert acct.device_code is None
    assert acct.oauth_token_kv_ref == ga._github_token_name(ACCOUNT_ID)


def test_token_from_a_different_github_user_is_refused(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _wire(monkeypatch, poll=_SUCCESS, who={"login": "mallory", "id": "99999"})

    out = ga.relogin_poll(ACCOUNT_ID, db=db)

    assert out.status == "failed"
    assert out.detail is not None
    assert "mallory" in out.detail and "alice" in out.detail
    # Nothing was touched: not the hub, not Key Vault.
    assert journal == []

    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    assert acct.oauth_token_kv_ref == "github-token-old"
    assert acct.github_login == "alice"
    assert acct.status is DeployStatus.READY


def test_hub_rejection_leaves_key_vault_untouched(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _wire(
        monkeypatch,
        poll=_SUCCESS,
        who={"login": "alice", "id": "12345"},
        install_error=RuntimeError("hub rejected the token (400): bad credentials"),
    )

    out = ga.relogin_poll(ACCOUNT_ID, db=db)

    assert out.status == "failed"
    assert out.detail is not None and "hub rejected" in out.detail
    assert journal == [("hub", "ghu_fresh")]  # no ("kv", ...) entry

    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    assert acct.oauth_token_kv_ref == "github-token-old"
    assert acct.status is DeployStatus.READY


def test_unreachable_hub_is_a_failure_not_a_500(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient network error must surface as a retryable failed status, not
    a stack trace — the caller is a polling UI."""
    journal = _wire(
        monkeypatch,
        poll=_SUCCESS,
        who={"login": "alice", "id": "12345"},
        install_error=httpx.ConnectError("connection refused"),
    )

    out = ga.relogin_poll(ACCOUNT_ID, db=db)

    assert out.status == "failed"
    assert journal == [("hub", "ghu_fresh")]


def test_pending_changes_nothing(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = _wire(monkeypatch, poll={"status": "pending"}, who={})

    out = ga.relogin_poll(ACCOUNT_ID, db=db)

    assert out.status == "pending"
    assert journal == []
    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    # The device code survives so the next poll continues the same flow.
    assert acct.device_code == "dev_code_123"


def test_unlabeled_account_accepts_any_login(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`whoami` is best-effort, so some accounts have no github_user_id. With
    nothing to compare against, refusing would make those accounts permanently
    unrecoverable — so the guard is skipped and the login gets recorded."""
    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    acct.github_user_id = None
    db.commit()
    _wire(monkeypatch, poll=_SUCCESS, who={"login": "bob", "id": "777"})

    out = ga.relogin_poll(ACCOUNT_ID, db=db)

    assert out.status == "success"
    assert out.github_login == "bob"


def test_non_ready_account_is_rejected(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deploying account owns `device_code` for its OWN flow; a re-login there
    would hijack it and confuse the deploy state machine."""
    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    acct.status = DeployStatus.DEPLOYING
    db.commit()
    _wire(monkeypatch, poll=_SUCCESS, who={"login": "alice", "id": "12345"})

    with pytest.raises(ga.HTTPException) as exc:
        ga.relogin_poll(ACCOUNT_ID, db=db)
    assert exc.value.status_code == 409


def test_poll_without_start_is_rejected(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    acct = db.get(GitHubAccount, ACCOUNT_ID)
    assert acct is not None
    acct.device_code = None
    db.commit()
    _wire(monkeypatch, poll=_SUCCESS, who={"login": "alice", "id": "12345"})

    with pytest.raises(ga.HTTPException) as exc:
        ga.relogin_poll(ACCOUNT_ID, db=db)
    assert exc.value.status_code == 409
