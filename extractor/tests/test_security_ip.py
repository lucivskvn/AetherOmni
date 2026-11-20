from django.test import RequestFactory, SimpleTestCase

from extractor.file_utils import get_client_ip


class ClientIPSecurityTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_direct_connection_returns_remote_addr(self):
        request = self.factory.get("/", REMOTE_ADDR="198.51.100.10")
        self.assertEqual(get_client_ip(request), "198.51.100.10")

    def test_untrusted_remote_addr_ignores_spoofed_xff(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="198.51.100.10",
            HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8",
        )
        self.assertEqual(get_client_ip(request), "198.51.100.10")

    def test_trusted_proxy_uses_rightmost_public_xff_ip(self):
        # Attacker sends X-Forwarded-For: 1.2.3.4; GFE/Cloud Run appends real IP 203.0.113.5
        request = self.factory.get(
            "/",
            REMOTE_ADDR="10.128.0.5",
            HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.5, 10.0.0.2",
        )
        self.assertEqual(get_client_ip(request), "203.0.113.5")

    def test_trusted_proxy_all_private_xff_returns_rightmost_valid(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="10.0.0.1, 10.0.0.2",
        )
        self.assertEqual(get_client_ip(request), "10.0.0.2")

    def test_malformed_xff_falls_back_to_remote_addr(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="not-an-ip, <script>",
        )
        self.assertEqual(get_client_ip(request), "127.0.0.1")
