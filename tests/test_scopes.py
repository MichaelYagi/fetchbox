from __future__ import annotations

from fetchbox.modules.auth import scopes as sc


def test_write_implies_read_and_star_implies_all() -> None:
    assert sc.has_scope({"media:write"}, "media:read")
    assert not sc.has_scope({"media:read"}, "media:write")
    assert sc.has_scope({"*"}, "auth:admin")
    assert not sc.has_scope({"system:write"}, "auth:admin")
    assert not sc.has_scope({"media:write"}, "library:read")


def test_can_grant_blocks_escalation() -> None:
    assert sc.can_grant({"*"}, ["*"]) == []
    assert sc.can_grant({"auth:admin", "media:write"}, ["media:read"]) == []
    assert sc.can_grant({"auth:admin", "media:read"}, ["media:write"]) == ["media:write"]
    assert sc.can_grant({"auth:admin", "media:write"}, ["*"]) == ["*"]


def test_normalize() -> None:
    assert sc.normalize(["media:read", "media:write", "system:read"]) == [
        "media:write",
        "system:read",
    ]
    assert sc.normalize(["media:read", "*"]) == ["*"]


def test_catalog_has_spec_scopes() -> None:
    for s in (
        "*",
        "media:read",
        "releases:write",
        "events:read",
        "auth:admin",
        "integrations:read",
        "notifications:write",
        "discovery:read",
    ):
        assert s in sc.CATALOG
    assert "discovery:write" not in sc.CATALOG
