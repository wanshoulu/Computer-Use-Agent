"""Logging — loguru-based file + stdout logging for the CUA server."""
import logging
import sys

from loguru import logger


class InterceptHandler(logging.Handler):
    """Route stdlib logging into loguru so third-party logs share one sink."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = None, 6
        # Walk up the stack until we leave the logging module itself
        f = sys._getframe(6)
        while f and f.f_code.co_filename == logging.__file__:
            f = f.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(level: str = "INFO") -> None:
    logger.remove()
    logger.add(sys.stdout, level=level, colorize=True)
    logger.add("logs/cua-server_{time:YYYY-MM-DD}.log", rotation="1 day", level=level, encoding="utf-8")
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
