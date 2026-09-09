#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys
import tempfile

from dotenv import load_dotenv

load_dotenv()

# Use private per-process cache directories when a deployment has not supplied
# an explicit cache location. Fixed paths below /tmp are shared and unsafe.
for cache_variable, cache_prefix in (
    ("HF_HOME", "aetheromni-hf-"),
    ("XDG_CACHE_HOME", "aetheromni-xdg-"),
    ("MPLCONFIGDIR", "aetheromni-mpl-"),
):
    os.environ.setdefault(cache_variable, tempfile.mkdtemp(prefix=cache_prefix))


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
