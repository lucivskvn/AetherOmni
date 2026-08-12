from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm


class TurnstileAuthenticationForm(AuthenticationForm):
    """Require the browser challenge before dispatching credentials to Supabase."""

    def clean(self):
        if getattr(settings, "CF_TURNSTILE_SITE_KEY", ""):
            token = self.request.POST.get("cf-turnstile-response", "").strip()
            if not token:
                raise forms.ValidationError(
                    "CAPTCHA verification is required. Please complete the security check and try again.",
                    code="captcha_required",
                )
        return super().clean()
