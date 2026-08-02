#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

# Configure specific temporary directories for libraries that need to write to the filesystem
# in serverless environments (like Cloud Run) where only /tmp is writable.
os.environ["HF_HOME"] = "/tmp/huggingface"  # nosec B108 # NOSONAR
os.environ["XDG_CACHE_HOME"] = "/tmp/xdg_cache"  # nosec B108 # NOSONAR
os.environ["MPLCONFIGDIR"] = "/tmp/matplotlib"  # nosec B108 # NOSONAR

from dotenv import load_dotenv


def main():
    """Run administrative tasks."""
    # Load .env file at startup
    load_dotenv()

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
