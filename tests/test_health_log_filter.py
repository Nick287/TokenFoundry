"""The health-probe access-log filter: drop the noise, keep everything else.

Container Apps polls /healthz forever and uvicorn logs a line per poll. On
dev-19 that was 18,442 of 22,492 control-plane log lines over 7 days — 82% of
what this service logged, and about half the workspace's total ingestion. It is
a fixed cost independent of traffic, so the quieter the environment, the more it
dominates.

The risk in a filter like this is not that it fails to drop the noise; it is
that it drops something real and nobody notices until an incident has no trail.
So most of these tests are about what must SURVIVE.
"""

import logging

from app.main import _DropHealthProbeAccessLogs


def _access_record(method: str, path: str, status: int = 200) -> logging.LogRecord:
    """A record shaped the way uvicorn.access actually emits one:
    '%s - "%s %s HTTP/%s" %d' % (client, method, path, version, status)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1:1234", method, path, "1.1", status),
        exc_info=None,
    )


F = _DropHealthProbeAccessLogs()


# --- what gets dropped -------------------------------------------------------


def test_health_probes_are_dropped():
    for path in ("/healthz", "/readyz", "/livez"):
        assert F.filter(_access_record("GET", path)) is False, path


# --- what must survive -------------------------------------------------------


def test_real_api_requests_still_log():
    assert F.filter(_access_record("GET", "/api/tenants")) is True
    assert F.filter(_access_record("POST", "/api/login")) is True


def test_a_path_merely_containing_healthz_still_logs():
    """Matched exactly, not by substring. A route like /api/healthz-config is a
    real endpoint and its calls are real traffic."""
    assert F.filter(_access_record("GET", "/api/healthz-config")) is True
    assert F.filter(_access_record("GET", "/healthz/detail")) is True


def test_records_that_are_not_access_lines_pass_through():
    """The filter is attached to uvicorn.access, but a defensive pass-through
    matters: anything whose args are not the expected 5-tuple must not be
    silently swallowed."""
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="something went wrong", args=None, exc_info=None,
    )
    assert F.filter(rec) is True

    short = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%s", args=("/healthz",), exc_info=None,
    )
    assert F.filter(short) is True


def test_a_failing_probe_is_also_dropped_from_access():
    """Deliberate: the access line carries no more information at 500 than at
    200, and Container Apps already reports an unhealthy revision. A real
    exception still reaches the uvicorn.error logger, which this filter is not
    attached to."""
    assert F.filter(_access_record("GET", "/healthz", status=500)) is False


def test_the_filter_is_installed_on_the_access_logger():
    """Guards the wiring, not just the class — an uninstalled filter passes
    every test above while changing nothing in production."""
    installed = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(f, _DropHealthProbeAccessLogs) for f in installed)
