import os
import tempfile
import unittest
from unittest.mock import patch

import main_agent
import tools_security


class SecurityReviewTests(unittest.TestCase):
    def test_lookalike_domain_is_flagged_without_network_access(self):
        report = tools_security.inspect_url("https://paypa1.example/login")

        self.assertTrue(any("resembles 'paypal'" in item["message"] for item in report["findings"]))
        self.assertIn("no network request", report["scanner"])

    def test_brand_in_deceptive_subdomain_is_flagged_but_official_subdomain_is_not(self):
        deceptive = tools_security.inspect_url("https://paypal.com.attacker.test/login")
        expected = tools_security.inspect_url("https://login.paypal.com/account")

        self.assertTrue(any("possible brand impersonation" in item["message"] for item in deceptive["findings"]))
        self.assertFalse(any("possible brand impersonation" in item["message"] for item in expected["findings"]))

    def test_pasted_code_flags_dynamic_execution_with_remediation(self):
        report = tools_security.review_safety(content="result = eval(input())", subject="pasted code")

        self.assertIn("CAUTION", report)
        self.assertIn("explicit parsing", report)
        self.assertIn("No file was executed", report)

    def test_file_signature_mismatch_is_flagged_and_not_quarantined(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as uploaded, \
                patch.object(tools_security.shutil, "which", return_value=None):
            uploaded.write(b"not a PDF")
            uploaded.flush()
            report = tools_security.inspect_file(uploaded.name)

        self.assertTrue(any("do not match" in item["message"] for item in report["findings"]))
        self.assertIn("not available", report["scanner"])

    def test_certificate_text_is_not_claimed_as_authenticated(self):
        report = tools_security.inspect_text(
            "Certificate: Advanced Engineering", subject="verify certificate")

        self.assertTrue(any("cannot be established" in item["message"] for item in report["findings"]))

    def test_explicit_attachment_request_uses_read_only_scanner(self):
        attachment = {"name": "report.pdf", "path": "/tmp/report.pdf", "text": "Certificate details"}
        with patch.object(tools_security, "review_safety", return_value="UNVERIFIED: report.pdf") as review:
            result = main_agent._parse_security_review("Is this certificate authentic?", "is this certificate authentic?", [attachment])

        self.assertIn("UNVERIFIED", result)
        review.assert_called_once_with(file_path="/tmp/report.pdf", content="Certificate details",
                                       subject="Is this certificate authentic?")

    def test_unrelated_attachment_request_is_not_intercepted(self):
        self.assertIsNone(main_agent._parse_security_review(
            "Summarize this document", "summarize this document", [{"name": "notes.txt"}]))


if __name__ == "__main__":
    unittest.main()