"""The hub-login poller — making an expired login visible.

`GitHubAccount.status` is a DEPLOY state machine. It reaches READY when the
container comes up and never moves again, so it cannot express "this hub stopped
working afterwards". A hub whose Copilot OAuth token expires keeps answering —
it returns 503 "Hub not logged in to Copilot" to every request — and that 503
sits inside the circuit breaker's 5xx range, so APIM sheds the hub for 60s,
re-admits it, sheds it again. Traffic does route around it, which is exactly why
nobody notices: the pool quietly runs at 2/3 capacity while all three accounts
show green on the page.

Everything needed to detect this had already shipped and was never wired up:
`/api/status` on the hub, `hub_client.fetch_status`, the `hub_status_*` columns,
and `hub_status_interval_seconds`. `fetch_status` had zero callers.

The property these tests exist to hold is the three-state one. "Logged in",
"login expired" and "we have not been able to ask" need different actions from
an operator — re-authorise, versus go and look at the deployment — so collapsing
the third into either of the others sends someone to do the wrong thing. In
particular an unreachable hub must NOT be recorded as logged out.

Hermetic: in-memory SQLite, hub HTTP and Key Vault stubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.hub_status_poller as poller
from app.models.enums import DeployStatus
from app.models.orm import Base, GitHubAccount
from app.services.hub_client import HubStatus

FQDN = "h-test.example.azurecontainerapps.io"


def _status(logged_in: bool = True, lost: int = 0, dropped: int = 0) -> HubStatus:
    return HubStatus(logged_in=logged_in, dropped=dropped, lost=lost,
                     audit_dropped=0, state="ok", reason=None)


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(poller, "SessionLocal", Session)
    monkeypatch.setattr(poller, "KeyVaultService",
                        lambda: type("_KV", (), {"get_secret": staticmethod(lambda _n: "tok")})())
    session = Session()
    session.add(GitHubAccount(id="gha_1", github_login="acct-one",
                              status=DeployStatus.READY, container_app_fqdn=FQDN))
    session.commit()
    yield session
    session.close()


def _acct(db: Any) -> GitHubAccount:
    db.expire_all()
    return db.get(GitHubAccount, "gha_1")


# --- the three states -------------------------------------------------------


def test_a_working_hub_is_recorded_as_logged_in(db: Any, monkeypatch) -> None:
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True))
    assert poller.poll_once()["logged_in"] == 1
    assert _acct(db).hub_logged_in is True
    assert _acct(db).hub_status_at is not None


def test_an_expired_login_is_recorded_as_false(db: Any, monkeypatch) -> None:
    """The whole point. Nothing else in the system changes state when this
    happens — `status` stays READY and the hub keeps answering."""
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(False))
    assert poller.poll_once()["expired"] == 1
    assert _acct(db).hub_logged_in is False


def test_an_unreachable_hub_is_not_recorded_as_logged_out(db: Any, monkeypatch) -> None:
    """"Could not ask" and "asked, and it said no" demand different actions:
    one sends the operator to re-authorise, the other to look at the deployment.
    Writing False here would send them to the wrong one."""
    monkeypatch.setattr(poller, "fetch_status", lambda *a: None)
    stats = poller.poll_once()
    assert stats["unreachable"] == 1
    assert _acct(db).hub_logged_in is None


def test_an_outage_does_not_erase_the_last_good_answer(db: Any, monkeypatch) -> None:
    """A stale answer with its timestamp beats a blank: the page can say
    "logged in, as of 40 minutes ago" instead of losing what it knew the moment
    the hub went quiet."""
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True))
    poller.poll_once()
    was = _acct(db).hub_status_at

    monkeypatch.setattr(poller, "fetch_status", lambda *a: None)
    poller.poll_once()
    assert _acct(db).hub_logged_in is True
    assert _acct(db).hub_status_at == was


def test_never_polled_stays_null(db: Any) -> None:
    """The default an existing row carries. It must not be False (which would
    claim every hub is broken) nor True (which would claim health never
    measured)."""
    assert _acct(db).hub_logged_in is None
    assert _acct(db).hub_status_at is None


# --- what else the poll carries ---------------------------------------------


def test_the_lost_counter_is_recorded(db: Any, monkeypatch) -> None:
    """`lost` is the billing-data-is-gone number — events given up on for good,
    each counted once. Before this poller nothing ever read it."""
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True, lost=7, dropped=21))
    poller.poll_once()
    assert _acct(db).usage_events_lost == 7
    assert _acct(db).usage_events_dropped == 21


def test_polling_is_an_overwrite_not_an_increment(db: Any, monkeypatch) -> None:
    """The hub reports cumulative counts since ITS process start. Adding them up
    across passes would multiply one loss into a growing fiction, and would also
    make the poller unsafe to run on several replicas."""
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True, lost=3))
    poller.poll_once()
    poller.poll_once()
    assert _acct(db).usage_events_lost == 3


# --- who gets polled --------------------------------------------------------


def test_accounts_that_are_not_ready_are_skipped(db: Any, monkeypatch) -> None:
    """A hub mid-deploy has nothing to report, and a failed one has no endpoint.
    Polling either would only produce noise."""
    db.add(GitHubAccount(id="gha_2", status=DeployStatus.DEPLOYING,
                         container_app_fqdn="h-2.example.com"))
    db.commit()
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True))
    assert poller.poll_once()["polled"] == 1


def test_an_account_with_no_endpoint_is_skipped(db: Any, monkeypatch) -> None:
    db.add(GitHubAccount(id="gha_3", status=DeployStatus.READY, container_app_fqdn=None))
    db.commit()
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True))
    assert poller.poll_once()["polled"] == 1


def test_one_bad_hub_does_not_stop_the_others(db: Any, monkeypatch) -> None:
    """This runs on a timer over every hub. A single unreachable one must not
    cost the rest their poll."""
    db.add(GitHubAccount(id="gha_2", status=DeployStatus.READY,
                         container_app_fqdn="h-2.example.com"))
    db.commit()
    monkeypatch.setattr(poller, "fetch_status",
                        lambda fqdn, _t: None if fqdn == FQDN else _status(True))
    stats = poller.poll_once()
    assert stats["polled"] == 2
    assert stats["unreachable"] == 1
    assert stats["logged_in"] == 1


def test_a_key_vault_failure_does_not_skip_the_poll(db: Any, monkeypatch) -> None:
    """/api/status is the hub's one unauthenticated route, so the admin token is
    a nicety. Losing it must not cost us the health signal."""
    def _boom():
        raise RuntimeError("kv down")

    monkeypatch.setattr(poller, "KeyVaultService",
                        lambda: type("_KV", (), {"get_secret": staticmethod(
                            lambda _n: _boom())})())
    monkeypatch.setattr(poller, "fetch_status", lambda *a: _status(True))
    db.query(GitHubAccount).filter_by(id="gha_1").update({"admin_token_kv_ref": "ref"})
    db.commit()
    assert poller.poll_once()["logged_in"] == 1


def test_the_poll_never_raises(db: Any, monkeypatch) -> None:
    """It runs on a background timer where an exception kills the loop for good
    — silently, and for the rest of the process's life."""
    def _explode(*a):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(poller, "fetch_status", _explode)
    poller.poll_once()  # must not raise


# --- staleness --------------------------------------------------------------


def test_a_stale_timestamp_is_left_for_the_reader_to_judge(db: Any, monkeypatch) -> None:
    """The poller records WHEN, and does not decide what counts as too old. The
    portal makes that call, so the threshold can change without a migration and
    without the stored answer meaning something different than it did."""
    old = datetime.now(UTC) - timedelta(hours=3)
    db.query(GitHubAccount).filter_by(id="gha_1").update(
        {"hub_logged_in": True, "hub_status_at": old})
    db.commit()
    monkeypatch.setattr(poller, "fetch_status", lambda *a: None)
    poller.poll_once()
    # Compared without tzinfo: the column is timestamptz on Postgres, but SQLite
    # has no timezone type and hands back a naive value. The instant is what this
    # test is about, so normalising here keeps it honest rather than asserting
    # something only the fake database decides.
    assert _acct(db).hub_status_at.replace(tzinfo=None) == old.replace(tzinfo=None)
