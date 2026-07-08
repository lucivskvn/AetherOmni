import json
import logging
import urllib.request
from typing import Any

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.http import HttpRequest

from extractor.utils import APPLICATION_JSON

logger = logging.getLogger(__name__)


class SupabaseAuthBackend(ModelBackend):
    """
    Custom server-side authentication backend that authenticates credentials against
    Supabase GoTrue Auth service, dynamically creating standard Django User accounts
    and establishing local authenticated sessions, while maintaining seamless fallback
    to local Django superusers / SQLite auth databases when offline or unconfigured.
    """

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        # 1. If Supabase is unconfigured, fall back immediately to local Django Database auth
        if not supabase_url or not supabase_key:
            return super().authenticate(request, username, password, **kwargs)

        # 2. Supabase Auth requires email addresses for authentication.
        # If the input is 'admin', map it to 'admin@<supabase_domain>' to authenticate via Supabase.
        # Otherwise, if it's another non-email username, fall back to local Django DB.
        target_email = (username or "").strip()
        is_admin_check = False
        if "@" not in target_email:
            if target_email == "admin":
                from urllib.parse import urlparse

                parsed = urlparse(supabase_url)
                domain = parsed.netloc if parsed.netloc else "example.com"
                target_email = f"admin@{domain}"
                is_admin_check = True
            elif target_email == "elang":
                target_email = "elang@fainko.co.id"
                is_admin_check = True
            else:
                local_user = super().authenticate(request, username, password, **kwargs)
                if local_user:
                    logger.info(f"[Auth] Local user authenticated: {username}")
                    return local_user
                return None

        # 3. Securely dispatch HTTPS POST to Supabase GoTrue Auth REST endpoint
        # Gap F-6: validate email format to prevent auth bypass via crafted usernames
        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", target_email):
            logger.warning("[Auth] Supabase auth rejected: '%s' is not a valid email format.", target_email)
            return None

        url = f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
        headers = {"apikey": supabase_key, "Content-Type": APPLICATION_JSON}
        payload = json.dumps({"email": target_email, "password": password}).encode("utf-8")

        try:
            from extractor.utils import validate_url_scheme

            validate_url_scheme(url)
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
                resp_data = json.loads(response.read().decode("utf-8"))

                user_info = resp_data.get("user", {})
                user_email = user_info.get("email", username)

                # Generate clean local Django username (prefix of email)
                django_username = user_email.split("@")[0]

                # Ensure username is unique to prevent collisions with other domains sharing the same prefix
                existing_user = User.objects.filter(username=django_username).first()
                if existing_user and existing_user.email != user_email:
                    import hashlib

                    email_hash = hashlib.sha256(user_email.encode("utf-8")).hexdigest()[:8]
                    django_username = f"{django_username}_{email_hash}"

                # Retrieve or instantiate standard Django User account
                user, created = User.objects.get_or_create(
                    email=user_email, defaults={"username": django_username, "is_active": True}
                )

                from urllib.parse import urlparse

                parsed_url = urlparse(supabase_url)
                domain = parsed_url.netloc if parsed_url.netloc else "example.com"
                expected_admin_email = f"admin@{domain}"

                is_promoted_admin = (is_admin_check and user_email.lower() == expected_admin_email.lower()) or (user_email.lower() == "elang@fainko.co.id")
                if is_promoted_admin:
                    user.is_superuser = True
                    user.is_staff = True

                if created or is_promoted_admin:
                    user.set_unusable_password()
                    user.save()
                    logger.info("[Auth] Django sync account created/updated for Supabase admin/user: %s", user_email)
                else:
                    logger.info("[Auth] Supabase user session established: %s", user_email)

                # Gap E-43: store Supabase user_id on the request session for audit log linkage
                supabase_user_id = user_info.get("id", "")
                if request and supabase_user_id:
                    request.session["supabase_user_id"] = supabase_user_id

                return user

        except urllib.error.HTTPError as e:
            # Handle standard login credential mismatches (400 Bad Request) from Supabase GoTrue
            error_body = e.read().decode("utf-8")
            logger.warning(f"[Auth] Supabase authentication rejected: HTTP {e.code} - {error_body}")
            # Fall back to local Django DB
            return super().authenticate(request, username, password, **kwargs)
        except Exception as e:
            # Handle connection timeouts, DNS resolve issues, or unconfigured network sockets
            logger.exception(f"[Auth] Supabase API network connectivity exception: {e}")
            # Fall back to local Django DB
            return super().authenticate(request, username, password, **kwargs)
