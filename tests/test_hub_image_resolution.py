"""Which image a NEW hub comes up on.

The image reaches a hub through `HUB_IMAGE_REF`, a repo variable written only
when an operator presses "推送 GitHub 部署配置". Nothing else re-publishes it —
not deploy.sh, not update-app.sh, not terraform — so it drifts, and the drift
hides: a stale tag is a valid string, so the hub deploys, pulls, and runs. It is
just running old code.

Measured on dev-19 (2026-08-26), the shape of the bug this pins:

    HUB_IMAGE_REF          gitmodel:v20260814044101   (published 08-14)
    the three live hubs    gitmodel:v20260825225134
    tags in the registry   7, six of them newer than the one configured

A new account added at that moment would have joined the fleet twelve days
behind it. `tests/test_hub_image_ref.py` already guards the EMPTY tag, which
fails loudly at pull time; these cover the tag that is present, well-formed, and
wrong — which no amount of inspecting the value can catch.

The other half of what is tested here is the fallback chain, and it matters more
than it looks: this code runs on the path that creates an account. If resolving
an image can fail that path, the fix is worse than the bug.

Hermetic — httpx and Key Vault are stubbed, no Azure.
"""

from __future__ import annotations

import httpx
import pytest

import app.services.acr as acr
import app.services.terraform_runner as tr

NEWEST = "v20260825225134"
TF_TAG = "v20260814044101"


# --------------------------------------------------------------------------- #
# acr.newest_tag                                                              #
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _Client:
    """Stands in for httpx.Client over the three-leg ACR token exchange."""

    def __init__(self, *, tags: list[dict] | None = None, fail_at: str | None = None) -> None:
        self._tags = tags if tags is not None else [{"name": NEWEST}]
        self._fail_at = fail_at
        self.tag_params: dict | None = None
        self.scope: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None

    def post(self, url: str, data: dict | None = None, **kw) -> _Resp:
        if url.endswith("/oauth2/exchange"):
            if self._fail_at == "exchange":
                return _Resp(401, {"errors": "denied"})
            return _Resp(200, {"refresh_token": "rt"})
        if self._fail_at == "token":
            return _Resp(403, {"errors": "no pull"})
        self.scope = (data or {}).get("scope")
        return _Resp(200, {"access_token": "at"})

    def get(self, url: str, params: dict | None = None, **kw) -> _Resp:
        if self._fail_at == "tags":
            return _Resp(404, {"errors": "no repo"})
        self.tag_params = params
        return _Resp(200, {"tags": self._tags})


@pytest.fixture
def _token(monkeypatch):
    """A syntactically real JWT whose `tid` claim can be decoded."""
    # {"tid": "11111111-1111-1111-1111-111111111111"}
    payload = "eyJ0aWQiOiAiMTExMTExMTEtMTExMS0xMTExLTExMTEtMTExMTExMTExMTExIn0"
    monkeypatch.setattr(acr, "_arm_token", lambda: f"hdr.{payload}.sig")


def _run(monkeypatch, client: _Client) -> str | None:
    monkeypatch.setattr(acr.httpx, "Client", lambda **kw: client)
    return acr.newest_tag("myacr", "gitmodel")


def test_returns_the_newest_tag(monkeypatch, _token):
    assert _run(monkeypatch, _Client()) == NEWEST


def test_ordering_is_the_registrys_not_ours(monkeypatch, _token):
    """`timedesc` is push time. Our tags happen to sort as strings because they
    are timestamps, but that is a naming convention — a hand-pushed tag would
    break a string sort while the registry's own ordering stays right."""
    c = _Client()
    _run(monkeypatch, c)
    assert c.tag_params == {"orderby": "timedesc", "n": 1}


def test_asks_for_the_scope_our_role_actually_grants(monkeypatch, _token):
    """AcrPull yields `pull`. `metadata_read` is the more obvious scope to reach
    for and also lists tags, but requesting it would need a role assignment the
    control plane does not have — verified against the live registry."""
    c = _Client()
    _run(monkeypatch, c)
    assert c.scope == "repository:gitmodel:pull"


@pytest.mark.parametrize("stage", ["exchange", "token", "tags"])
def test_every_leg_failing_yields_none_not_an_exception(monkeypatch, _token, stage):
    """This runs while an account is being created. A registry problem must
    degrade to the fallback, never abort the deploy."""
    assert _run(monkeypatch, _Client(fail_at=stage)) is None


def test_empty_repository_yields_none(monkeypatch, _token):
    assert _run(monkeypatch, _Client(tags=[])) is None


def test_a_transport_error_yields_none(monkeypatch, _token):
    def _boom(**kw):
        raise httpx.ConnectError("dns")

    monkeypatch.setattr(acr.httpx, "Client", _boom)
    assert acr.newest_tag("myacr", "gitmodel") is None


def test_an_unusable_arm_token_yields_none(monkeypatch):
    """Belt and braces on the `tid` decode: a malformed token must not escape as
    a stack trace out of a background deploy task."""
    monkeypatch.setattr(acr, "_arm_token", lambda: "not-a-jwt")
    assert acr.newest_tag("myacr", "gitmodel") is None


# --------------------------------------------------------------------------- #
# terraform_runner._refresh_hub_image_ref                                     #
# --------------------------------------------------------------------------- #


class _Settings:
    acr_name = "myacr"
    hub_image_tag = TF_TAG
    github_repo_owner = "o"
    github_repo_name = "r"


def _wire(monkeypatch, *, newest: str | None, pat: str | None = "pat",
          settings: object | None = None) -> list[tuple[str, str]]:
    """Returns the (name, value) pairs actually published."""
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(tr, "get_settings", lambda: settings or _Settings())
    monkeypatch.setattr(tr.acr, "newest_tag", lambda *a: newest)
    monkeypatch.setattr(tr, "KeyVaultService", lambda: type(
        "_KV", (), {"get_secret": staticmethod(lambda _n: pat)})())
    monkeypatch.setattr(tr.httpx, "Client", lambda **kw: type(
        "_C", (), {"__enter__": lambda s: s, "__exit__": lambda s, *a: None})())
    monkeypatch.setattr(tr, "GitHubRepoConfigurator", lambda *a: type(
        "_G", (), {"set_variable": staticmethod(
            lambda _hc, n, v: written.append((n, v)))})())
    return written


def test_publishes_the_registrys_newest(monkeypatch):
    written = _wire(monkeypatch, newest=NEWEST)
    tr._refresh_hub_image_ref()
    assert written == [("HUB_IMAGE_REF", f"gitmodel:{NEWEST}")]


def test_falls_back_to_the_tag_terraform_injected(monkeypatch):
    """Stale, but valid and pullable — strictly better than leaving a value that
    is even older."""
    written = _wire(monkeypatch, newest=None)
    tr._refresh_hub_image_ref()
    assert written == [("HUB_IMAGE_REF", f"gitmodel:{TF_TAG}")]


def test_writes_nothing_when_no_tag_can_be_found(monkeypatch):
    """Publishing `gitmodel:` here would turn an out-of-date hub into one that
    cannot start at all — the exact unpullable ref test_hub_image_ref.py exists
    to prevent. Leaving the variable alone keeps today's behaviour."""
    class _NoTag(_Settings):
        hub_image_tag = ""

    written = _wire(monkeypatch, newest=None, settings=_NoTag())
    tr._refresh_hub_image_ref()
    assert written == []


def test_a_missing_bootstrap_pat_is_a_skip_not_a_failure(monkeypatch):
    """The deploy PAT can dispatch workflows but is scoped out of the variables
    API (403 Resource not accessible by personal access token, verified
    2026-08-26), so this needs the bootstrap PAT — which an operator may
    legitimately never have set."""
    written = _wire(monkeypatch, newest=NEWEST, pat=None)
    tr._refresh_hub_image_ref()
    assert written == []


def test_a_github_failure_does_not_propagate(monkeypatch):
    _wire(monkeypatch, newest=NEWEST)

    def _raise(*a):
        raise tr.GitHubRepoError("403")

    monkeypatch.setattr(tr, "GitHubRepoConfigurator", lambda *a: type(
        "_G", (), {"set_variable": staticmethod(_raise)})())
    tr._refresh_hub_image_ref()  # must not raise


def test_no_registry_configured_still_falls_back(monkeypatch):
    class _NoAcr(_Settings):
        acr_name = ""

    written = _wire(monkeypatch, newest=None, settings=_NoAcr())
    tr._refresh_hub_image_ref()
    assert written == [("HUB_IMAGE_REF", f"gitmodel:{TF_TAG}")]


def test_refresh_happens_before_the_dispatch(monkeypatch):
    """Order is the whole point. The workflow reads HUB_IMAGE_REF when it starts,
    so a refresh landing after the dispatch would apply to the NEXT account and
    this bug would look half-fixed — new hubs one deploy behind instead of many."""
    order: list[str] = []
    monkeypatch.setattr(tr, "_new_admin_token", lambda: "a")
    monkeypatch.setattr(tr, "_new_hub_key", lambda: "k")
    monkeypatch.setattr(tr, "_write_jobinput", lambda *a: None)
    monkeypatch.setattr(tr, "_refresh_hub_image_ref", lambda: order.append("refresh"))
    monkeypatch.setattr(tr, "_trigger_workflow", lambda *a: order.append("dispatch"))
    monkeypatch.setattr(tr, "_find_run", lambda *a: 1)
    monkeypatch.setattr(tr, "_poll_run", lambda *a: None)
    monkeypatch.setattr(tr, "_read_state_outputs",
                        lambda _a: {"app_url": "u", "resource_group": "g"})

    tr.deploy_hub("gha_x", "tok")
    assert order == ["refresh", "dispatch"]
