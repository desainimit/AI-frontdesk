import json
import logging

from app.core.logging import JsonFormatter, request_id_var


def test_logging_allowlist_and_request_correlation() -> None:
    token = request_id_var.set("request-123")
    try:
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "request_completed", (), None
        )
        record.password = "do-not-log"
        record.body = "medical-details"
        record.status_code = 200
        output = JsonFormatter().format(record)
        assert json.loads(output)["request_id"] == "request-123"
        assert json.loads(output)["status_code"] == 200
        assert "do-not-log" not in output
        assert "medical-details" not in output
    finally:
        request_id_var.reset(token)


def test_third_party_messages_and_exception_text_are_not_logged() -> None:
    exc = ValueError("secret-database-url")
    record = logging.LogRecord(
        "sqlalchemy.engine",
        logging.ERROR,
        __file__,
        1,
        "secret-sql-value",
        (),
        (ValueError, exc, None),
    )
    output = JsonFormatter().format(record)
    assert "secret" not in output
    assert json.loads(output)["error_type"] == "ValueError"
