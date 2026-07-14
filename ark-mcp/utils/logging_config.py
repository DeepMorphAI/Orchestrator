# Copyright © 2026 Jiahao Cai and Bin Ni.
# All Rights Reserved.
# This source code and all proprietary algorithms contained herein
# are the exclusive intellectual property of the authors.
# No part of this code may be reproduced, distributed, or modified
# without express written permission from the authors.
"""Shared logging configuration for the whole project."""

from __future__ import annotations

import datetime
import logging
import os
import shlex
import sys
from pathlib import Path

import colorlog

_DEFAULT_FORMAT = "%(log_color)s%(levelname)-8s%(reset)s %(asctime)s | %(name)s | %(message_log_color)s%(message)s%(reset)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONSOLE_HANDLER_NAME = "arkagents_console"
_FILE_HANDLER_NAME = "arkagents_file"

_LOG_COLORS = {
    "DEBUG": "cyan",
    "INFO": "green",
    "WARNING": "bold_yellow",
    "ERROR": "bold_red",
    "CRITICAL": "bold_white,bg_red",
}

_MESSAGE_COLORS = {
    "WARNING": "yellow",
    "ERROR": "bold_red",
    "CRITICAL": "bold_white,bg_red",
}


def _find_handler(root_logger: logging.Logger, name: str) -> logging.Handler | None:
    for handler in root_logger.handlers:
        if handler.get_name() == name:
            return handler
    return None


def _build_log_path() -> Path:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return Path("logs") / f"arkagents_{timestamp}_{os.getpid()}.log"


def _write_log_header(log_path: Path) -> None:
    command = " ".join(shlex.quote(arg) for arg in sys.argv)
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"# started_at: {started_at}\n")
        log_file.write(f"# cwd: {os.getcwd()}\n")
        log_file.write(f"# command: {command}\n\n")


def setup_logging(
    level: int | str = logging.INFO,
    *,
    force: bool = False,
    write_to_file: bool = True,
) -> None:
    """
    Configure root logging with an optional file handler for all logs and a
    terminal handler for warnings and above.

    This function is idempotent by default and won't duplicate handlers.
    """
    root_logger = logging.getLogger()

    if force:
        for handler in list(root_logger.handlers):
            handler.close()
        root_logger.handlers.clear()

    console_handler = _find_handler(root_logger, _CONSOLE_HANDLER_NAME)
    if console_handler is None:
        console_handler = colorlog.StreamHandler()
        console_handler.set_name(_CONSOLE_HANDLER_NAME)
        root_logger.addHandler(console_handler)
    # When a file handler captures INFO+, the console only needs to surface
    # warnings so it stays uncluttered. When there's no file, the console is
    # the only sink so it must show everything down to `level`.
    console_handler.setLevel(level if not write_to_file else logging.WARNING)
    console_handler.setFormatter(
        colorlog.ColoredFormatter(
            _DEFAULT_FORMAT,
            datefmt="%H:%M:%S",
            log_colors=_LOG_COLORS,
            secondary_log_colors={"message": _MESSAGE_COLORS},
        )
    )

    file_handler = _find_handler(root_logger, _FILE_HANDLER_NAME)
    if write_to_file:
        if file_handler is None:
            log_path = _build_log_path()
            _write_log_header(log_path)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.set_name(_FILE_HANDLER_NAME)
            root_logger.addHandler(file_handler)
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter(_FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
        )
    elif file_handler is not None:
        file_handler.close()
        root_logger.removeHandler(file_handler)

    root_logger.setLevel(level)
