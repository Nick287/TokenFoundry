"""Account teardown ordering — the gate that keeps a failure recoverable.

`_teardown_account` runs pool-member -> backend -> Azure resources -> KV + DB
row. The first step used to be best-effort, and that is how a hub became an
orphan in production: the APIM cleanup 400'd, teardown carried on regardless,
terraform destroyed the Container App and the account row was deleted. What was
left was a backend still wired into a live pool pointing at nothing, and no
record to retry from — the only way out was hand-editing ARM.

The distinction these tests protect is not "did we log the error" but "is the
system still in a state the user can act on". A failure that leaves everything
intact is an inconvenience; one that destroys the evidence and the retry handle
is an outage.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.api.github_accounts as gh
from app.models.enums import DeployStatus


class _Acct:
    def __init__(self) -> None:
        self.id = "gha_test"
        self.backend_ids = ["llm-openai-gha_test"]
        self.oauth_token_kv_ref = "gh-gha-test-oauth"
        self.hub_key_kv_ref = None
        self.admin_token_kv_ref = None
        self.status = DeployStatus.DELETING
        self.error_detail: str | None = None


class _Db:
    def __init__(self, acct: _Acct) -> None:
        self.acct = acct
        self.deleted: list[Any] = []
        self.commits = 0

    def get(self, _model: Any, _pk: str) -> _Acct:
        return self.acct

    def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Teardown with every collaborator stubbed and its calls recorded."""
    acct = _Acct()
    db = _Db(acct)
    seen: dict[str, Any] = {"destroy": 0, "kv_deletes": [], "pool_removed": 0}

    monkeypatch.setattr(gh, "SessionLocal", lambda: db)

    class _Prov:
        def remove_hub_from_pools(self, _aid: str, _bids: list[str]) -> None:
            seen["pool_removed"] += 1
            if seen.get("pool_raises"):
                raise RuntimeError("400 Bad Request: At least 1 service")

    class _Kv:
        def get_secret(self, _ref: str) -> str:
            return "tok"

        def delete_secret(self, ref: str) -> None:
            seen["kv_deletes"].append(ref)

    def _destroy(_aid: str, _tok: str) -> None:
        seen["destroy"] += 1

    monkeypatch.setattr(gh, "ApimProvisioner", _Prov)
    monkeypatch.setattr(gh, "KeyVaultService", _Kv)
    monkeypatch.setattr(gh.terraform_runner, "destroy_hub", _destroy)
    return acct, db, seen


def test_happy_path_destroys_and_removes_the_row(wired) -> None:
    acct, db, seen = wired
    gh._teardown_account("gha_test")

    assert seen["pool_removed"] == 1
    assert seen["destroy"] == 1
    assert db.deleted == [acct]


def test_apim_failure_destroys_nothing(wired) -> None:
    """The core guarantee: a failed APIM cleanup must not reach terraform."""
    _acct, _db, seen = wired
    seen["pool_raises"] = True

    gh._teardown_account("gha_test")

    assert seen["destroy"] == 0, "destroyed the hub after APIM cleanup failed"
    assert seen["kv_deletes"] == [], "deleted KV secrets after APIM cleanup failed"


def test_apim_failure_keeps_the_account_row_for_a_retry(wired) -> None:
    """Without the row there is nothing to press delete on a second time."""
    _acct, db, seen = wired
    seen["pool_raises"] = True

    gh._teardown_account("gha_test")

    assert db.deleted == [], "deleted the account row, making the orphan unfixable"


def test_apim_failure_marks_the_account_failed_with_a_reason(wired) -> None:
    acct, _db, seen = wired
    seen["pool_raises"] = True

    gh._teardown_account("gha_test")

    assert acct.status == DeployStatus.FAILED
    assert acct.error_detail is not None
    # The message has to say the environment is untouched, or the operator's
    # next move is a manual hunt for half-deleted resources.
    assert "nothing was destroyed" in acct.error_detail


def test_missing_account_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EmptyDb(_Db):
        def get(self, _model: Any, _pk: str) -> None:  # type: ignore[override]
            return None

    db = _EmptyDb(_Acct())
    monkeypatch.setattr(gh, "SessionLocal", lambda: db)
    gh._teardown_account("gha_missing")
    assert db.deleted == []
