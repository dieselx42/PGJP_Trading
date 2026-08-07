"""Structured logging and secret redaction."""

from __future__ import annotations

import json
import logging

import pytest

from app.config import Config
from app.logging_config import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    correlation_scope,
    get_correlation_id,
    mask_account_id,
    redact_text,
    redact_value,
)
from tests.conftest import permissive_env


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "PASSWORD",
            "api_key",
            "apiKey",
            "ibkr_password",
            "github_token",
            "authorization",
            "session_cookie",
            "ssh_private_key",
            "aws_secret_access_key",
        ],
    )
    def test_sensitive_keys_are_redacted(self, key: str) -> None:
        assert redact_value(key, "hunter2") == REDACTED

    def test_nested_structures_are_redacted(self) -> None:
        payload = {"outer": {"password": "hunter2", "safe": "visible"}}
        result = redact_value("payload", payload)
        assert result == {"outer": {"password": REDACTED, "safe": "visible"}}

    def test_lists_are_redacted(self) -> None:
        assert redact_value("token", ["a", "b"]) == REDACTED

    @pytest.mark.parametrize(
        "text",
        [
            "password=hunter2",
            "api_key: abcdef123456",
            "Authorization: Bearer abcdefghijklmnop",
        ],
    )
    def test_credential_shaped_text_is_redacted(self, text: str) -> None:
        assert "hunter2" not in redact_text(text)
        assert REDACTED in redact_text(text)

    def test_github_tokens_are_redacted(self) -> None:
        assert "ghp_" not in redact_text("token ghp_abcdefghijklmnopqrstuvwxyz0123456789")

    def test_private_keys_are_redacted(self) -> None:
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
        assert "AAAA" not in redact_text(key)

    def test_ordinary_text_is_untouched(self) -> None:
        message = "connected to IB Gateway at 127.0.0.1:4002 as client 10"
        assert redact_text(message) == message

    def test_deeply_nested_structures_hit_the_depth_limit(self) -> None:
        payload: dict[str, object] = {"a": "leaf"}
        for _ in range(12):
            payload = {"a": payload}
        # The point is that it terminates rather than recursing forever.
        assert redact_value("root", payload) is not None


class TestAccountMasking:
    @pytest.mark.parametrize(
        ("account", "expected"),
        [
            ("DU1234567", "DU***567"),
            ("U7654321", "U7***321"),
            (None, "-"),
            ("", "-"),
            ("ABC", "***"),
        ],
    )
    def test_masking(self, account: str | None, expected: str) -> None:
        assert mask_account_id(account) == expected


class TestJsonFormatter:
    def test_record_is_valid_json_with_required_fields(self) -> None:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        record.event = "test.event"
        record.run_id = "run_1"
        record.correlation_id = "cor_1"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "hello"
        assert payload["event"] == "test.event"
        assert payload["run_id"] == "run_1"
        assert payload["correlation_id"] == "cor_1"
        assert payload["ts"].endswith("+00:00"), "timestamps must be explicit UTC"

    def test_extras_are_preserved(self) -> None:
        record = logging.LogRecord(
            name="app.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=(),
            exc_info=None,
        )
        record.symbol = "MSL"
        record.quantity = 2
        payload = json.loads(JsonFormatter().format(record))
        assert payload["symbol"] == "MSL"
        assert payload["quantity"] == 2

    def test_filter_redacts_extras_on_the_record(self) -> None:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="connecting",
            args=(),
            exc_info=None,
        )
        record.ibkr_password = "hunter2"
        RedactionFilter().filter(record)
        assert record.ibkr_password == REDACTED


class TestCorrelationContext:
    def test_scope_sets_and_restores(self) -> None:
        with correlation_scope("cor_abc"):
            assert get_correlation_id() == "cor_abc"
        assert get_correlation_id() != "cor_abc"

    def test_nested_scopes_restore_correctly(self) -> None:
        with correlation_scope("outer"):
            with correlation_scope("inner"):
                assert get_correlation_id() == "inner"
            assert get_correlation_id() == "outer"


def test_configure_logging_writes_a_rotating_file(tmp_path) -> None:
    config = Config.from_env(
        permissive_env(LOG_DIR=str(tmp_path), LOG_LEVEL="INFO", LOG_FORMAT="json")
    )
    logger = configure_logging(config, run_id="run_test")
    logger.info("hello", extra={"event": "test", "password": "hunter2"})
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_file = tmp_path / "trading.log"
    assert log_file.exists()
    contents = log_file.read_text()
    assert "hunter2" not in contents, "a secret reached the log file"
    assert "run_test" in contents
