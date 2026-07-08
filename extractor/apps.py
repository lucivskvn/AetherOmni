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

        # Only run in the main process (avoids double-scheduling in gunicorn's
        # prefork model and Django test runner).
        if os.environ.get("RUN_MAIN") == "true" or os.environ.get("GUNICORN_WORKER"):
            return

        # Mark that we are the main process so child gunicorn workers don't
        # re-schedule.
        os.environ["GUNICORN_WORKER"] = "1"

        self._start_periodic_reaper()
        self._start_periodic_cleanup()

    # ──────────────────────────────────────────────────────────────────────────

    def _start_periodic_reaper(self) -> None:
        """Runs reap_stale_tasks every 5 minutes in a daemon thread."""

        def _loop() -> None:
            import time

            # Initial delay so DB is fully ready before first run
            time.sleep(60)
            while True:
                try:
                    from extractor.tasks import reap_stale_tasks

                    reaped = reap_stale_tasks()
                    if reaped:
                        logger.info("[AppReady] Reaped %s stuck document task(s).", reaped)
                except Exception:
                    logger.exception("[AppReady] reap_stale_tasks raised an exception.")
                time.sleep(300)  # every 5 minutes

        t = threading.Thread(target=_loop, daemon=True, name="stale-task-reaper")
        t.start()
        logger.info("[AppReady] Stale-task reaper thread started (interval: 5 min).")

    def _start_periodic_cleanup(self) -> None:
        """Runs cleanup_expired_documents_task every 60 minutes in a daemon thread."""

        def _loop() -> None:
            import time

            time.sleep(120)  # initial delay
            while True:
                try:
                    from extractor.tasks import cleanup_expired_documents_task

                    cleanup_expired_documents_task()
                except Exception:
                    logger.exception("[AppReady] cleanup_expired_documents_task raised an exception.")
                time.sleep(3600)  # every hour

        t = threading.Thread(target=_loop, daemon=True, name="doc-cleanup")
        t.start()
        logger.info("[AppReady] Document cleanup thread started (interval: 60 min).")
