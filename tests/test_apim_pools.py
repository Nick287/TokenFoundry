"""Backend-pool membership — the path that orphaned a hub in production.

Deleting the only GitHub account left its backend wired into a live pool while
its Container App was destroyed underneath. One request in three then
round-robined into a hub that no longer existed. Two independent defects lined
up to produce that, and each gets pinned here:

1. ARM refuses an empty pool —

       ValidationError: At least 1 service and at most 30 services should be
       identified for the backend pool.

   (reproduced against the deployed APIM). Removing the last member therefore
   has to DELETE the pool, not write `services: []`.

2. Teardown treated step 1 as best-effort and carried on to destroy the hub and
   delete the account row, so the failure was not merely unfixed — it became
   unfixable through the app, because nothing was left to retry from.

The fake below asserts on the ARM calls actually issued: a unit test can witness
"we sent DELETE, not PUT-with-empty-services", which is the whole question here.
What ARM does with those calls was settled by live reproduction, not guessed at.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from azure.core.exceptions import ResourceNotFoundError

from app.services.apim_provisioner import ApimProvisioner

_POOL_URL_PART = "/backends/llm-openai-pool"


def _member(backend_id: str) -> dict[str, Any]:
    # ARM stores service ids as RELATIVE paths, which is what the suffix match
    # in the provisioner has to cope with.
    return {
        "id": f"/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement"
        f"/service/apim/backends/{backend_id}",
        "priority": 1,
        "weight": 1,
    }


class _FakeArm:
    """Records every request; replies from a scripted pool state."""

    def __init__(self, members: list[str] | None, get_status: int = 200) -> None:
        self.members = members
        self.get_status = get_status
        self.calls: list[tuple[str, str, dict | None]] = []

    # -- httpx.Client protocol (only what the provisioner uses) --
    def __enter__(self) -> _FakeArm:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def _resp(self, method: str, url: str, status: int, payload: dict | None = None):
        request = httpx.Request(method, url)
        return httpx.Response(
            status, json=payload if payload is not None else {}, request=request
        )

    def get(self, url: str, headers: dict | None = None):
        self.calls.append(("GET", url, None))
        if self.get_status != 200:
            return self._resp("GET", url, self.get_status)
        return self._resp(
            "GET",
            url,
            200,
            {
                "properties": {
                    "description": "Hub pool for llm-openai",
                    "type": "Pool",
                    "pool": {
                        "services": [_member(m) for m in (self.members or [])],
                        "sessionAffinity": {
                            "sessionId": {"source": "Cookie", "name": "SessionId"}
                        },
                    },
                }
            },
        )

    def put(self, url: str, headers: dict | None = None, json: dict | None = None):
        self.calls.append(("PUT", url, json))
        return self._resp("PUT", url, 200)

    def delete(self, url: str, headers: dict | None = None):
        self.calls.append(("DELETE", url, None))
        return self._resp("DELETE", url, 200)

    # -- assertions --
    def methods(self) -> list[str]:
        return [m for m, _u, _b in self.calls]

    def last_put_services(self) -> list[dict]:
        for method, _url, body in reversed(self.calls):
            if method == "PUT" and body:
                return body["properties"]["pool"]["services"]
        raise AssertionError("no PUT was issued")


@pytest.fixture
def prov(monkeypatch: pytest.MonkeyPatch) -> ApimProvisioner:
    p = ApimProvisioner.__new__(ApimProvisioner)
    monkeypatch.setattr(ApimProvisioner, "_arm_token", lambda self: "token")
    monkeypatch.setattr(
        ApimProvisioner,
        "_backend_base",
        lambda self: "https://management.azure.com/subscriptions/s/resourceGroups/rg"
        "/providers/Microsoft.ApiManagement/service/apim/backends",
    )
    return p


def _wire(monkeypatch: pytest.MonkeyPatch, fake: _FakeArm) -> None:
    monkeypatch.setattr(
        "app.services.apim_provisioner.httpx.Client", lambda **_kw: fake
    )


# --------------------------------------------------------------------------- #
# Defect 1: the last member                                                    #
# --------------------------------------------------------------------------- #
def test_removing_the_last_member_deletes_the_pool(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARM rejects `services: []`, so emptying the pool must not be attempted."""
    fake = _FakeArm(members=["llm-openai-gha_only"])
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_only")

    assert fake.methods() == ["GET", "DELETE"]
    assert _POOL_URL_PART in fake.calls[-1][1]


def test_removing_the_last_member_never_puts_an_empty_services_list(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression stated directly: this exact PUT is what ARM 400s on."""
    fake = _FakeArm(members=["llm-openai-gha_only"])
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_only")

    for method, _url, body in fake.calls:
        if method == "PUT" and body:
            assert body["properties"]["pool"]["services"], (
                "PUT an empty services list — ARM rejects this with "
                "ValidationError and teardown then orphans the backend"
            )


# --------------------------------------------------------------------------- #
# Defect 1b: the policy that referenced the pool                               #
#                                                                              #
# The first fix was incomplete and shipped anyway. Deleting the pool is legal   #
# only once nothing references it, and the API policy ALWAYS references it —    #
# `<set-backend-service backend-id="llm-openai-pool"/>` IS the routing. So      #
# removing a provider's last hub was a guaranteed 400:                          #
#                                                                               #
#   ValidationError: Backend 'llm-openai-pool' is used by the following         #
#   entities: /apis/llm-openai;rev=1/policies/policy                            #
#                                                                               #
# It slipped through because the empty-pool experiment used a scratch pool that #
# no API referenced, where DELETE succeeds.                                     #
# --------------------------------------------------------------------------- #
def test_last_member_detaches_the_api_policy_before_deleting_the_pool(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    fake = _FakeArm(members=["llm-openai-gha_only"])
    _wire(monkeypatch, fake)
    monkeypatch.setattr(
        ApimProvisioner, "_detach_api_policy",
        lambda self, provider: order.append(f"detach:{provider}"),
    )
    real_delete = fake.delete

    def tracking_delete(url: str, headers: dict | None = None):
        order.append("delete-pool")
        return real_delete(url, headers)

    fake.delete = tracking_delete  # type: ignore[method-assign]

    prov._pool_remove_service(
        "llm-openai-pool", "llm-openai-gha_only", provider="openai"
    )

    # Order is the whole point: DELETE first and ARM rejects it.
    assert order == ["detach:openai", "delete-pool"]


def test_removing_a_non_last_member_leaves_the_policy_alone(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool survives, so it is still referenced — and must stay routable.
    Detaching here would break a live provider on an ordinary account removal."""
    detached: list[str] = []
    fake = _FakeArm(members=["llm-openai-gha_a", "llm-openai-gha_b"])
    _wire(monkeypatch, fake)
    monkeypatch.setattr(
        ApimProvisioner, "_detach_api_policy",
        lambda self, provider: detached.append(provider),
    )

    prov._pool_remove_service(
        "llm-openai-pool", "llm-openai-gha_a", provider="openai"
    )

    assert detached == []


def test_absent_pool_does_not_detach_the_policy(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 pool means teardown already ran. Deleting the policy on a retry
    would break an API that a LATER account had since repaired."""
    detached: list[str] = []
    fake = _FakeArm(members=[], get_status=404)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(
        ApimProvisioner, "_detach_api_policy",
        lambda self, provider: detached.append(provider),
    )

    prov._pool_remove_service(
        "llm-openai-pool", "llm-openai-gha_only", provider="openai"
    )

    assert detached == []


def test_teardown_passes_the_provider_through_to_the_pool_removal(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_pool_remove_service` cannot detach what it was never told about — the
    provider has to be threaded from `remove_hub_from_pools`, and a default of
    None silently restores the old broken behaviour."""
    seen: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        ApimProvisioner, "_pool_remove_service",
        lambda self, pool, be, provider=None: seen.append((pool, be, provider)),
    )
    monkeypatch.setattr(ApimProvisioner, "remove_backend", lambda self, be: None)

    prov.remove_hub_from_pools("gha_x")

    assert seen == [
        ("llm-openai-pool", "llm-openai-gha_x", "openai"),
        ("llm-anthropic-pool", "llm-anthropic-gha_x", "anthropic"),
        ("llm-google-pool", "llm-google-gha_x", "google"),
    ]


def test_detaching_an_already_gone_policy_is_not_an_error(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown is retried after a partial failure; the second run must not turn
    a 404 into a hard stop that blocks the retry it exists to serve."""
    class _Boom:
        def delete(self, *_a: object, **_kw: object) -> None:
            raise ResourceNotFoundError("gone")

    # `client` is a lazily-built property, so it has to be patched on the CLASS.
    monkeypatch.setattr(
        ApimProvisioner, "client",
        property(lambda self: SimpleNamespace(api_policy=_Boom())),
    )
    monkeypatch.setattr(ApimProvisioner, "_rg", "rg", raising=False)
    monkeypatch.setattr(ApimProvisioner, "_service", "apim", raising=False)

    prov._detach_api_policy("openai")  # must not raise


def test_unknown_provider_detaches_nothing(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id outside PROVIDER_APIS has no API to detach; reaching for cfg would
    KeyError inside teardown, which is fail-open on the destructive path."""
    class _Explode:
        def delete(self, *_a: object, **_kw: object) -> None:
            raise AssertionError("must not touch a policy for an unknown provider")

    monkeypatch.setattr(
        ApimProvisioner, "client",
        property(lambda self: SimpleNamespace(api_policy=_Explode())),
    )
    monkeypatch.setattr(ApimProvisioner, "_rg", "rg", raising=False)
    monkeypatch.setattr(ApimProvisioner, "_service", "apim", raising=False)

    prov._detach_api_policy("not-a-provider")


def test_removing_one_of_several_still_puts_the_remainder(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeArm(members=["llm-openai-gha_a", "llm-openai-gha_b"])
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_a")

    assert fake.methods() == ["GET", "PUT"]
    kept = fake.last_put_services()
    assert len(kept) == 1
    assert kept[0]["id"].endswith("llm-openai-gha_b")


def test_session_affinity_survives_a_removal(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing affinity would silently cold-start prompt caching on every turn."""
    fake = _FakeArm(members=["llm-openai-gha_a", "llm-openai-gha_b"])
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_a")

    body = fake.calls[-1][2]
    assert body["properties"]["pool"]["sessionAffinity"]["sessionId"]["source"] == "Cookie"


# --------------------------------------------------------------------------- #
# Idempotence — teardown reruns after a failed attempt                         #
# --------------------------------------------------------------------------- #
def test_absent_pool_is_a_no_op(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeArm(members=None, get_status=404)
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_a")

    assert fake.methods() == ["GET"]


def test_member_not_in_pool_is_a_no_op(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry after a partial teardown must not delete a pool it doesn't own."""
    fake = _FakeArm(members=["llm-openai-gha_other"])
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_gone")

    assert fake.methods() == ["GET"]


def test_member_match_is_case_insensitive(
    prov: ApimProvisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARM may echo a different casing than we construct; a case-sensitive
    compare would silently leave the member behind."""
    fake = _FakeArm(members=["LLM-OpenAI-GHA_A", "llm-openai-gha_b"])
    _wire(monkeypatch, fake)

    prov._pool_remove_service("llm-openai-pool", "llm-openai-gha_a")

    assert fake.methods() == ["GET", "PUT"]
    assert len(fake.last_put_services()) == 1


# --------------------------------------------------------------------------- #
# Defect 3: ARM's explanation must not be discarded                            #
# --------------------------------------------------------------------------- #
def test_arm_error_body_is_carried_into_the_exception() -> None:
    """A bare "400 Bad Request" in the logs cost a live reproduction to recover
    a message ARM had already sent."""
    request = httpx.Request("PUT", "https://management.azure.com/x")
    resp = httpx.Response(
        400,
        json={"error": {"code": "ValidationError", "message": "At least 1 service"}},
        request=request,
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        ApimProvisioner._raise_for_arm(resp)
    assert "ValidationError" in str(excinfo.value)
    assert "At least 1 service" in str(excinfo.value)


def test_successful_response_does_not_raise() -> None:
    request = httpx.Request("PUT", "https://management.azure.com/x")
    ApimProvisioner._raise_for_arm(httpx.Response(200, json={}, request=request))
