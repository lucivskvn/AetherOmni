from django.test import RequestFactory, SimpleTestCase, override_settings

from extractor.forms import TurnstileAuthenticationForm


class TurnstileAuthenticationFormTestCase(SimpleTestCase):
    """Unit tests for TurnstileAuthenticationForm captcha enforcement."""

    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(CF_TURNSTILE_SITE_KEY="dummy-turnstile-key")
    def test_missing_captcha_token_raises_validation_error(self):
        request = self.factory.post("/login/", data={"username": "user", "password": "pwd"})
        form = TurnstileAuthenticationForm(request=request, data={"username": "user", "password": "pwd"})
        self.assertFalse(form.is_valid())
        self.assertIn("CAPTCHA verification is required", form.errors.get("__all__", [""])[0])

    @override_settings(CF_TURNSTILE_SITE_KEY="")
    def test_disabled_turnstile_skips_captcha_check(self):
        request = self.factory.post("/login/", data={"username": "user", "password": "pwd"})
        form = TurnstileAuthenticationForm(request=request, data={"username": "user", "password": "pwd"})
        # Will fail username/password auth check, but NOT on captcha_required
        self.assertFalse(form.is_valid())
        errors = form.errors.get("__all__", [])
        self.assertNotIn("CAPTCHA verification is required", " ".join(errors))
