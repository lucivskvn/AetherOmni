import os

from django.core.wsgi import get_wsgi_application
from dotenv import load_dotenv

# Load .env file at startup
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

application = get_wsgi_application()
