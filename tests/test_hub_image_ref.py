"""HUB_IMAGE_REF must never be published with an empty tag.

`hub_image_tag` used to default to "latest", which reads like "newest" but is an
ordinary tag name that nothing in this repo pushes — verified against the live
registry, which answers `manifest tagged by "latest" is not found`. So the
default named an image that could not exist, and the failure surfaced minutes
later inside a GitHub Actions hub deploy as a pull error.

The default is now empty, which moves the problem rather than solving it unless
the empty case is caught: `f"gitmodel:{''}"` yields the ref `gitmodel:`, equally
unpullable and even less legible. This pins the guard that turns it into a 409
at the moment the operator presses the button, while they can still act on it.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.api.deploy_config as dc


def _settings(tag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    s = dc.get_settings()
    monkeypatch.setattr(s, "hub_image_tag", tag, raising=False)
    monkeypatch.setattr(dc, "get_settings", lambda: s)


def test_empty_tag_is_rejected_before_anything_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings("", monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        dc._repo_variables()
    assert excinfo.value.status_code == 409
    # The message must name the env var, or the operator has nowhere to start.
    assert "TF_HUB_IMAGE_TAG" in excinfo.value.detail


def test_a_real_tag_is_published_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings("v20260804150145", monkeypatch)
    assert dc._repo_variables()["HUB_IMAGE_REF"] == "gitmodel:v20260804150145"


def test_the_ref_is_never_a_bare_repo_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gitmodel:` and `gitmodel` are both unpullable; neither may escape."""
    _settings("v1", monkeypatch)
    ref = dc._repo_variables()["HUB_IMAGE_REF"]
    assert ref.startswith("gitmodel:")
    assert ref != "gitmodel:"
