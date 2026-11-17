"""automate.security: scrypt passwords, RFC 6238 TOTP with replay protection, recovery codes, tokens, masking."""
import base64
import unittest
from urllib.parse import parse_qs, urlparse

from automate import security

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 appendix B, SHA-1


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        h = security.hash_password("korrekt pferd batterie")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertNotEqual(h, security.hash_password("korrekt pferd batterie"))  # per-password salt
        self.assertTrue(security.verify_password("korrekt pferd batterie", h))
        self.assertFalse(security.verify_password("korrekt pferd batteriE", h))
        self.assertFalse(security.verify_password("x", "not-a-hash"))
        self.assertFalse(security.verify_password(None, h))

    def test_minimum_length_in_german(self):
        with self.assertRaises(ValueError) as e:
            security.hash_password("kurz")
        self.assertIn("mindestens 10 Zeichen", str(e.exception))


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        for t, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
            self.assertEqual(security.totp_now(RFC_SECRET, t), code)

    def test_window_and_replay_protection(self):
        t = 1_800_000_000
        step = int(t // 30)
        code = security.totp_now(RFC_SECRET, t)
        self.assertEqual(security.verify_totp(RFC_SECRET, code, None, t=t), step)
        self.assertEqual(security.verify_totp(RFC_SECRET, f" {code[:3]} {code[3:]} ", 0, t=t + 30), step)  # 1 step late
        self.assertIsNone(security.verify_totp(RFC_SECRET, code, step, t=t))  # already used
        self.assertIsNone(security.verify_totp(RFC_SECRET, code, 0, t=t + 90))  # too late
        self.assertIsNone(security.verify_totp(RFC_SECRET, "12345", 0, t=t))
        self.assertIsNone(security.verify_totp(RFC_SECRET, "abcdef", 0, t=t))

    def test_secret_uri_and_qr(self):
        secret = security.new_totp_secret()
        self.assertEqual(len(secret), 32)
        self.assertTrue(security.verify_totp(secret, security.totp_now(secret), None))
        uri = security.otpauth_uri("owner", secret)
        parsed = urlparse(uri)
        self.assertEqual((parsed.scheme, parsed.netloc), ("otpauth", "totp"))
        self.assertEqual(parse_qs(parsed.query)["secret"], [secret])
        self.assertEqual(parse_qs(parsed.query)["issuer"], ["GTA Clipper"])
        svg = security.qr_svg(uri)
        self.assertTrue(svg.startswith("<svg") and "viewBox" in svg)
        # the dashboard shows it as an <img> data: URL, which renders only with the SVG namespace
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg.split(">", 1)[0])
        import xml.etree.ElementTree as ET
        self.assertEqual(ET.fromstring(svg).tag, "{http://www.w3.org/2000/svg}svg")


class CodeTokenMaskTests(unittest.TestCase):
    def test_recovery_codes_work_once(self):
        plain, hashed = security.new_recovery_codes()
        self.assertEqual((len(plain), len(set(plain)), len(hashed)), (10, 10, 10))
        self.assertRegex(plain[0], r"^[a-z2-9]{5}-[a-z2-9]{5}$")
        left = security.use_recovery_code(plain[3].upper().replace("-", " "), hashed)
        self.assertEqual(len(left), 9)
        self.assertIsNone(security.use_recovery_code(plain[3], left))
        self.assertIsNone(security.use_recovery_code("nope", hashed))

    def test_session_tokens_and_mask(self):
        token, token_hash = security.new_session_token()
        self.assertEqual(security.hash_token(token), token_hash)
        self.assertEqual(len(token_hash), 64)
        self.assertNotEqual(security.new_session_token()[0], token)
        self.assertEqual(security.mask("sk-ant-api03-ABCDEFGHIJKL"), "sk-a…IJKL")
        self.assertEqual((security.mask("short"), security.mask(None)), ("…", ""))


if __name__ == "__main__":
    unittest.main()
