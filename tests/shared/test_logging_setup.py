"""setup_logging tests: handler attachment, idempotency on repeat calls,
level updates, and formatting.

Each test builds its own standalone ``logging.Logger`` instance (not
``logging.getLogger(name)``, which registers into the shared global manager)
so tests never share state with each other or with the real root logger.
"""

import io
import logging

from agent_parity.shared.logging_setup import setup_logging


def test_attaches_a_stream_handler():
    logger = logging.Logger("test-attach")
    setup_logging(logger=logger)
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)


def test_sets_the_requested_level():
    logger = logging.Logger("test-level")
    setup_logging(logging.DEBUG, logger=logger)
    assert logger.level == logging.DEBUG


def test_second_call_does_not_duplicate_handler():
    logger = logging.Logger("test-idempotent")
    setup_logging(logger=logger)
    setup_logging(logger=logger)
    setup_logging(logger=logger)
    assert len(logger.handlers) == 1


def test_second_call_still_updates_level():
    logger = logging.Logger("test-level-update")
    setup_logging(logging.INFO, logger=logger)
    setup_logging(logging.WARNING, logger=logger)
    assert logger.level == logging.WARNING
    assert len(logger.handlers) == 1  # level changed, handler still not duplicated


def test_different_logger_instances_configure_independently():
    logger_a = logging.Logger("test-independent-a")
    logger_b = logging.Logger("test-independent-b")
    setup_logging(logger=logger_a)
    setup_logging(logger=logger_b)
    assert len(logger_a.handlers) == 1
    assert len(logger_b.handlers) == 1


def test_writes_formatted_output_to_given_stream():
    logger = logging.Logger("test-output")
    stream = io.StringIO()
    setup_logging(logger=logger, stream=stream, fmt="%(levelname)s:%(message)s")
    logger.error("boom")
    assert stream.getvalue().strip() == "ERROR:boom"


def test_defaults_to_root_logger_when_none_given():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        result = setup_logging()
        assert result is root
    finally:
        # Restore the real root logger exactly as found — this is the one
        # test here that must touch shared global state.
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        if hasattr(root, "_shared_tools_logging_configured"):
            delattr(root, "_shared_tools_logging_configured")
