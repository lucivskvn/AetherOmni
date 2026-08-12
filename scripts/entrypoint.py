"""Shell-free production startup for the Cloud Run web service."""

from __future__ import annotations

import os
import subprocess  # nosec B404 -- required for fixed, shell-free process control
import sys
from typing import NoReturn


def _debug_enabled() -> bool:
    return os.getenv("DJANGO_DEBUG", "False").lower() == "true"


def _gunicorn_command() -> list[str]:
    workers = os.getenv("GUNICORN_WORKERS", "2")
    if workers not in {"1", "2", "3", "4"}:
        workers = "2"
    command = [
        "gunicorn",
        "--bind",
        ":8080",
        "--workers",
        workers,
        "--threads",
        "4",
        "--timeout",
        "120",
    ]
    if not _debug_enabled():
        command.extend(["--log-level", "warning"])
    return [*command, "core.wsgi:application"]


def _run_migrations() -> None:
    subprocess.run(  # nosec B603 -- interpreter, script, and arguments are constants
        [sys.executable, "manage.py", "migrate", "--noinput"], check=True
    )


def _start_database_initialization() -> None:
    subprocess.Popen(  # nosec B603 -- interpreter and bootstrap script are constants
        [sys.executable, "init_surreal.py"]
    )


def main() -> NoReturn:
    _run_migrations()
    _start_database_initialization()
    command = _gunicorn_command()
    os.execvp(command[0], command)  # noqa: S606  # nosec B606 -- executable and arguments are constructed from constants


if __name__ == "__main__":
    main()
