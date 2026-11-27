from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm


class TurnstileAuthenticationForm(AuthenticationForm):
    """Require the browser challenge before dispatching credentials to Supabase."""

    def clean(self):
        if getattr(settings, "CF_TURNSTILE_SITE_KEY", "") and self.request and hasattr(self.request, "POST"):
            token = self.request.POST.get("cf-turnstile-response", "").strip()
            if not token:
                raise forms.ValidationError(
                    "CAPTCHA verification is required. Please complete the security check and try again.",
                    code="captcha_required",
                )
        try:
            return super().clean()
        except forms.ValidationError as exc:
            # AUD-392: a Supabase 5xx/network failure is an outage, not bad
            # credentials — don't tell the user their password is wrong.
            if exc.code == "invalid_login" and getattr(self.request, "_supabase_auth_unavailable", False):
                raise forms.ValidationError(
                    "The authentication service is temporarily unavailable. Please try again later.",
                    code="auth_unavailable",
                ) from exc
            raise
