from __future__ import annotations

from fetchbox.core.logging import REDACTED, redact_processor, redact_string


def test_redacts_secret_keys_and_nested() -> None:
    out = redact_processor(
        None,
        "info",
        {"event": "x", "password": "hunter2", "settings": {"api_key": "abc", "url": "http://h"}},
    )
    assert out["password"] == REDACTED
    assert out["settings"]["api_key"] == REDACTED
    assert out["settings"]["url"] == "http://h"


def test_redacts_patterns_in_strings() -> None:
    s = redact_string(
        "GET https://t.example/announce/0123456789abcdef0123?x=1 "
        "and https://idx.example/api?apikey=SECRET123&t=search "
        "with Authorization: Bearer eyJhbGci.payload "
        "key fb_ab12cd34_" + "z" * 40 + " "
        "and http://user:pw@host/"
    )
    assert "0123456789abcdef0123" not in s
    assert "SECRET123" not in s
    assert "eyJhbGci" not in s
    assert "fb_ab12cd34_" + REDACTED in s
    assert "z" * 40 not in s
    assert ":pw@" not in s
    assert "t=search" in s


def test_empty_secret_left_alone() -> None:
    out = redact_processor(None, "info", {"event": "x", "token": None})
    assert out["token"] is None
