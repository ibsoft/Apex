"""EMAIL pack attachments: --attach builds a multipart message with the real
file, reports the names, and refuses missing or oversized files. smtp_connect
is stubbed so nothing touches the network or the state database."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

MAIL_PY = Path(__file__).resolve().parents[1] / "data" / "skills" / "email_manager" / "mail.py"

_spec = importlib.util.spec_from_file_location("email_mail", MAIL_PY)
mail = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mail)


class _FakeSmtp:
    def __init__(self):
        self.sent = None

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent = msg
        return {}  # nothing refused

    def quit(self):
        pass


class SendAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "report.docx"
        self.doc.write_bytes(b"fake-docx-bytes")
        self.cfg = {"smtp_from": "me@example.com", "pop3_email": "me@example.com"}
        self.fake = _FakeSmtp()
        self._orig_connect = mail.smtp_connect
        self._orig_record = mail.record_sent
        mail.smtp_connect = lambda cfg: self.fake
        mail.record_sent = lambda *a, **k: None

    def tearDown(self):
        mail.smtp_connect = self._orig_connect
        mail.record_sent = self._orig_record
        self.tmp.cleanup()

    def test_attach_document_becomes_a_multipart_attachment(self):
        mail.cmd_send(self.cfg, ["me@example.com"], "Report", "Here it is.",
                      None, False, [str(self.doc)])
        msg = self.fake.sent
        self.assertEqual(msg["Subject"], "Report")
        self.assertEqual(len(msg.get_payload()), 2)  # text + attachment
        attachment = msg.get_payload()[1]
        self.assertEqual(attachment.get_filename(), "report.docx")
        self.assertEqual(attachment.get_payload(decode=True), b"fake-docx-bytes")

    def test_multiple_attachments_are_all_kept(self):
        second = Path(self.tmp.name) / "notes.txt"
        second.write_text("hello", encoding="utf-8")
        mail.cmd_send(self.cfg, ["me@example.com"], "Both", "See attached.",
                      None, False, [str(self.doc), str(second)])
        names = sorted(p.get_filename() for p in self.fake.sent.get_payload()[1:])
        self.assertEqual(names, ["notes.txt", "report.docx"])

    def test_missing_attach_raises_before_sending(self):
        with self.assertRaises(mail.MailError):
            mail.cmd_send(self.cfg, ["me@example.com"], "X", "body", None, False,
                          [str(Path(self.tmp.name) / "nope.pdf")])

    def test_combined_size_cap_is_enforced(self):
        big = Path(self.tmp.name) / "big.bin"
        big.write_bytes(b"z" * (mail.MAX_ATTACH_TOTAL + 1))
        with self.assertRaises(mail.MailError):
            mail.cmd_send(self.cfg, ["me@example.com"], "X", "body", None, False,
                          [str(big)])


if __name__ == "__main__":
    unittest.main()