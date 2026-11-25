from __future__ import annotations

import logging
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ExtractorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "extractor"

    def ready(self) -> None:
        """
        Called once when Django starts.
        Schedules periodic background tasks that must run automatically:
          - reap_stale_tasks  : every 5 minutes, recovers EXTRACTING/REFINING/EMBEDDING
                                documents that are stuck due to worker restarts.
          - cleanup_expired_documents_task : every 60 minutes, purges expired docs.
        These are implemented as lightweight daemon threads so they add zero Cloud
        Tasks cost and run even if Cloud Tasks are not configured.
        """
        import os
        import sys

        from django.conf import settings

        if not getattr(settings, "ENABLE_PERIODIC_MAINTENANCE", False):
            return

        # Only run in the main process (avoids double-scheduling in gunicorn's
        # prefork model and Django test runner).
        if os.environ.get("RUN_MAIN") == "true" or os.environ.get("GUNICORN_WORKER") or "test" in sys.argv:
            return

        # Gunicorn workers each import the app post-fork, so an env marker set
        # here cannot propagate to siblings. Hold an advisory lock for the
        # process lifetime instead: the first process wins, the rest skip.
        lock_fd = self._acquire_maintenance_lock()
        if lock_fd is None:
            return
        type(self)._maintenance_lock_fd = lock_fd

        self._start_periodic_reaper()
        self._start_periodic_cleanup()

    _maintenance_lock_fd: int | None = None

    @classmethod
    def _acquire_maintenance_lock(cls) -> int | None:
        try:
            import fcntl
            import os
            import tempfile

            lock_path = os.path.join(tempfile.gettempdir(), "korda-periodic-maintenance.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return None
            return fd
        except ImportError, OSError:
            # Platforms without fcntl (or unwritable temp dirs) keep the
            # previous single-process behaviour rather than disabling maintenance.
            return -1

    # ──────────────────────────────────────────────────────────────────────────

    _shutdown_event = threading.Event()

    def _start_periodic_reaper(self) -> None:
        """Runs reap_stale_tasks every 5 minutes in a daemon thread."""

        def _loop() -> None:
            # Initial delay so DB is fully ready before first run
            if self._shutdown_event.wait(60):
                return
            while not self._shutdown_event.is_set():
                try:
                    from extractor.tasks import reap_stale_tasks

                    reaped = reap_stale_tasks()
                    if reaped:
                        logger.info("[AppReady] Reaped %s stuck document task(s).", reaped)
                except Exception as loop_err:
                    logger.exception("[AppReady] reap_stale_tasks encountered error: %s", loop_err)
                if self._shutdown_event.wait(300):
                    break

        t = threading.Thread(target=_loop, daemon=True, name="stale-task-reaper")
        t.start()
        logger.info("[AppReady] Stale-task reaper thread started (interval: 5 min).")

    def _start_periodic_cleanup(self) -> None:
        """Runs cleanup_expired_documents_task every 60 minutes in a daemon thread."""

        def _loop() -> None:
            if self._shutdown_event.wait(120):  # initial delay
                return
            while not self._shutdown_event.is_set():
                try:
                    from extractor.tasks import cleanup_expired_documents_task

                    cleanup_expired_documents_task()
                except Exception as cleanup_err:
                    logger.exception("[AppReady] cleanup_expired_documents_task encountered error: %s", cleanup_err)
                if self._shutdown_event.wait(3600):
                    break

        t = threading.Thread(target=_loop, daemon=True, name="doc-cleanup")
        t.start()
        logger.info("[AppReady] Document cleanup thread started (interval: 60 min).")
