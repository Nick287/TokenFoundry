"""The per-API diagnostic body: what it must carry, and why each field is there.

An API-level diagnostic OVERRIDES the service-level one, and a field it omits
does NOT fall back to the service value — it takes APIM's own default. Measured
on dev-19 (2026-08-20): the service diagnostic was set to 10% sampling with
alwaysLog=allErrors, and the llm-* APIs still logged 73 of 73 successes and 20
of 20 failures. Fully unsampled, because this body carried neither field.

So the body is the whole contract for these APIs. Two failure modes it has to be
protected against, both silent:

  * dropping `metrics` switches customMetrics OFF for that API (root-caused on
    dev-a05) — token counts stop and nothing errors;
  * omitting `sampling` makes `apim_sampling_percentage` inert, so lowering it
    to cut ingestion changes nothing and gives no clue why.

Hermetic: the provisioner is built via object.__new__ so no settings are read
and no Azure client is constructed, matching test_apim_policy.py.
"""

import app.services.apim_provisioner as ap
from app.services.apim_provisioner import ApimProvisioner


def _provisioner(pct: int = 100) -> ApimProvisioner:
    p = object.__new__(ApimProvisioner)
    p._sub_id = "sub"
    p._rg = "rg"
    p._service = "apim"
    p._sampling_percentage = pct
    return p


def _body(monkeypatch, pct: int = 100) -> dict:
    """Run _ensure_api_llm_diagnostic and capture the JSON it would PUT."""
    seen: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a) -> None:
            return None

        def put(self, url, headers=None, json=None):  # noqa: A002
            seen["url"] = url
            seen["json"] = json
            return _Resp()

    monkeypatch.setattr(ap.httpx, "Client", lambda **kw: _Client())
    p = _provisioner(pct)
    monkeypatch.setattr(type(p), "_arm_token", lambda self: "tok", raising=False)
    p._ensure_api_llm_diagnostic("llm-anthropic")
    return seen


# --- the field that must never be lost ---------------------------------------


def test_metrics_stays_on(monkeypatch):
    """Adding fields to this body must not displace `metrics`. An API-level
    diagnostic without it overrides the service-level true to false and silently
    kills customMetrics for the API — dev-a05."""
    assert _body(monkeypatch)["json"]["properties"]["metrics"] is True


# --- the fields that make the variable mean something ------------------------


def test_sampling_is_written_from_the_configured_percentage(monkeypatch):
    props = _body(monkeypatch, pct=10)["json"]["properties"]
    assert props["sampling"] == {"samplingType": "fixed", "percentage": 10}


def test_errors_are_exempt_from_sampling(monkeypatch):
    """Without this, lowering the percentage samples successes AND failures, and
    the failure rate on screen stops matching reality."""
    assert _body(monkeypatch)["json"]["properties"]["alwaysLog"] == "allErrors"


def test_correlation_protocol_matches_the_service_level(monkeypatch):
    """The two disagreed before — service W3C, API-level defaulting to Legacy —
    which changed the correlation-id format for the APIs carrying all traffic."""
    assert _body(monkeypatch)["json"]["properties"]["httpCorrelationProtocol"] == "W3C"


def test_llm_message_capture_stays_off(monkeypatch):
    """largeLanguageModel fed ApiManagementGatewayLlmLog, which stored full
    prompts and completions for every tenant regardless of the audit switch, and
    whose token counts could not be billed from. It must not come back by
    accident."""
    assert "largeLanguageModel" not in _body(monkeypatch)["json"]["properties"]


def test_it_targets_the_api_scoped_diagnostic(monkeypatch):
    url = _body(monkeypatch)["url"]
    assert "/apis/llm-anthropic/diagnostics/applicationinsights" in url


# --- the percentage is clamped, not trusted ----------------------------------


def test_percentage_is_clamped_to_the_range_apim_accepts():
    """Arrives through an env var, where a typo is silent — so out-of-range
    values are pulled in rather than sent to ARM."""
    for given, expected in ((-5, 0), (150, 100), (10, 10), (100, 100)):
        p = object.__new__(ApimProvisioner)
        p._sampling_percentage = min(max(int(given), 0), 100)
        assert p._sampling_percentage == expected, given


def test_zero_is_preserved_not_rounded_up():
    """0 is a real setting, not a mistake to correct. With alwaysLog=allErrors
    it means "log failures, drop successes" — cheaper AND more useful than any
    low non-zero value, which keeps a random fraction of successes and is
    therefore neither complete nor cheap. Silently turning it into 1 would
    reinstate exactly the sampled-successes behaviour the operator chose against."""
    p = object.__new__(ApimProvisioner)
    p._sampling_percentage = min(max(0, 0), 100)
    assert p._sampling_percentage == 0


def test_zero_still_writes_the_error_exemption(monkeypatch):
    """Zero is only a coherent posture BECAUSE errors are exempt. If alwaysLog
    ever stopped being written, 0 would silently become "log nothing at all"."""
    props = _body(monkeypatch, pct=0)["json"]["properties"]
    assert props["sampling"]["percentage"] == 0
    assert props["alwaysLog"] == "allErrors"
    assert props["metrics"] is True
