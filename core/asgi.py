import logging
import os
import tempfile

from django.core.asgi import get_asgi_application
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env file at startup
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
hf_dir = os.getenv("HF_HOME") or os.path.join(tempfile.gettempdir(), "hf_home")
os.environ.setdefault("HF_HOME", hf_dir)
try:
    os.makedirs(os.environ["HF_HOME"], exist_ok=True)
except OSError as exc:
    logger.debug("HF_HOME directory initialization skipped: %s", exc)

application = get_asgi_application()
