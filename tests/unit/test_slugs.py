from __future__ import annotations

import pytest

from herdr_workflow.errors import WorkflowError
from herdr_workflow.workflows import slugs


@pytest.mark.parametrize("slug", ["auth", "auth-refresh", "auth_refresh", "a1"])
def test_valid_slugs_are_accepted(slug: str) -> None:
    assert slugs.validate(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", ".", "..", "../outside", "/tmp/outside", "nested/path", "Uppercase", "has space"],
)
def test_unsafe_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(WorkflowError, match="invalid slug"):
        slugs.validate(slug)


def test_slugs_have_a_length_limit() -> None:
    with pytest.raises(WorkflowError, match="invalid slug"):
        slugs.validate("a" * 65)
