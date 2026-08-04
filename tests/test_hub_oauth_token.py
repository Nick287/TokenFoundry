"""Hub-side OAuth token resolution and hot-swap.

Two behaviours that are invisible until they fail in production, and fail
silently when they do:

1. **The store must win over the environment.** `COPILOT_OAUTH_TOKEN` is a
   Container App secret injected at deploy time, so in the cloud it is ALWAYS
   set. When the environment was read first, every runtime login wrote SQLite
   and was then ignored — the portal said "logged in" while the hub went on
   401-ing against a dead token. Nothing raised; nothing logged. Only a test
   pinning the order keeps that from coming back.

2. **A rejected token must leave the hub exactly as it was.** `install_oauth_token`
   is reached from a UI button, and the hub it targets may still be serving
   happily on a cached (not yet expired) API token. A bad paste taking that hub
   down would make the recovery button more dangerous than the outage it fixes.

Everything here is hermetic: a temp SQLite file for the store, and a stubbed
exchange so no GitHub call is made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# The hub ships vendored (it deploys as its own container), so it isn't on the
# path as an installed package.
_HUB_ROOT = Path(__file__).resolve().parent.parent / "vendored" / "gitmodel-hub"
if str(_HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_HUB_ROOT))

cc = pytest.importorskip(
    "hub.copilot_client",
    reason="vendored hub deps not installed in this environment",
)
store = pytest.importorskip("hub.store")


class _FakeSettings:
    """Just the two attributes `store._conn` and `copilot_client` actually read."""

    def __init__(self, db_path: Path, oauth_token: str = "") -> None:
        self.db_path = db_path
        self.journal_mode = "DELETE"  # WAL needs shared memory; a temp file may not have it
        self.copilot_oauth_token = oauth_token


@pytest.fixture()
def hub_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real (empty) store on disk plus a settable env token.

    Deliberately a real SQLite database rather than a fake store: the bug being
    guarded is about which of two REAL sources wins, so faking one of them out
    would test the wrong thing.
    """
    settings = _FakeSettings(tmp_path / "hub.db")
    monkeypatch.setattr(store, "get_settings", lambda: settings)
    monkeypatch.setattr(cc, "get_settings", lambda: settings)
    store.init_db()
    cc._api_token_mem.clear()
    yield settings
    cc._api_token_mem.clear()


# --------------------------------------------------------------------------- #
# Resolution order                                                             #
# --------------------------------------------------------------------------- #
def test_env_token_used_when_store_empty(hub_env: _FakeSettings) -> None:
    """Cold start: SQLite lives in /tmp, so a fresh revision has an empty store
    and must fall back to the token terraform just injected."""
    hub_env.copilot_oauth_token = "ghu_from_env"
    assert cc.get_oauth_token() == "ghu_from_env"
    assert cc.is_authenticated() is True


def test_store_token_wins_over_env(hub_env: _FakeSettings) -> None:
    """THE regression. Both set means someone logged in after this process
    started, so the store copy is by definition the newer of the two."""
    hub_env.copilot_oauth_token = "ghu_stale_deploy_time"
    store.set_oauth_token("ghu_installed_at_runtime")
    assert cc.get_oauth_token() == "ghu_installed_at_runtime"


def test_falls_back_to_env_after_store_cleared(hub_env: _FakeSettings) -> None:
    """Logging out drops the runtime token; the deploy-time one is still valid
    configuration, so the hub keeps working rather than going dark."""
    hub_env.copilot_oauth_token = "ghu_from_env"
    store.set_oauth_token("ghu_installed_at_runtime")
    store.clear_oauth_token()
    assert cc.get_oauth_token() == "ghu_from_env"


def test_no_token_anywhere_raises(hub_env: _FakeSettings) -> None:
    hub_env.copilot_oauth_token = ""
    assert cc.is_authenticated() is False
    with pytest.raises(cc.NotAuthenticatedError):
        cc.get_oauth_token()


# --------------------------------------------------------------------------- #
# install_oauth_token                                                          #
# --------------------------------------------------------------------------- #
def _stub_exchange(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[str | None]:
    """Replace the GitHub exchange. Records the token the store held at the
    moment of the call, which is how we prove the store was written BEFORE the
    token was validated (and therefore that the rollback has something to undo).
    """
    seen: list[str | None] = []

    async def _fake() -> tuple[str, str, float]:
        seen.append(store.get_oauth_token())
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cc, "_exchange_for_api_token", _fake)
    return seen


async def test_install_persists_and_caches(
    hub_env: _FakeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_env.copilot_oauth_token = "ghu_deploy_time"
    seen = _stub_exchange(monkeypatch, ("tid_new", "https://api.example", 9_999.0))

    out = await cc.install_oauth_token("  ghu_fresh  ")

    assert out == {"ok": True, "api_token_expires_at": 9_999.0}
    # Validated the NEW token, not the one it replaced.
    assert seen == ["ghu_fresh"]
    assert store.get_oauth_token() == "ghu_fresh"
    assert cc.get_oauth_token() == "ghu_fresh"
    # The cached API token is replaced too — otherwise the hub would keep using
    # the one exchanged from the dead token until it expired on its own.
    assert cc._api_token_mem["token"] == "tid_new"


async def test_rejected_token_rolls_back_everything(
    hub_env: _FakeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub still serving on a cached API token must survive a bad paste."""
    store.set_oauth_token("ghu_old_but_working")
    cc._api_token_mem.update(token="tid_live", endpoint="https://api.example", expires_at=8_888.0)
    _stub_exchange(monkeypatch, RuntimeError("401 from GitHub"))

    with pytest.raises(RuntimeError):
        await cc.install_oauth_token("ghu_bad")

    assert store.get_oauth_token() == "ghu_old_but_working"
    assert cc._api_token_mem == {
        "token": "tid_live",
        "endpoint": "https://api.example",
        "expires_at": 8_888.0,
    }


async def test_rejected_token_restores_the_empty_store(
    hub_env: _FakeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback from "nothing stored" must clear, not write back a None — else
    the env fallback would be shadowed by a store row holding garbage."""
    hub_env.copilot_oauth_token = "ghu_deploy_time"
    _stub_exchange(monkeypatch, RuntimeError("401 from GitHub"))

    with pytest.raises(RuntimeError):
        await cc.install_oauth_token("ghu_bad")

    assert store.get_oauth_token() is None
    assert cc.get_oauth_token() == "ghu_deploy_time"


@pytest.mark.parametrize("bad", ["", "   "])
async def test_blank_token_rejected_before_any_write(
    hub_env: _FakeSettings, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    store.set_oauth_token("ghu_old_but_working")
    seen = _stub_exchange(monkeypatch, ("tid_new", "https://api.example", 1.0))

    with pytest.raises(ValueError):
        await cc.install_oauth_token(bad)

    assert seen == []  # never even tried GitHub
    assert store.get_oauth_token() == "ghu_old_but_working"
