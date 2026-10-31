"""Tests for redact.py. Run: python -m unittest test_redact -v   (no network, no Phoenix)"""

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import redact
from redact import ConfigError, RedactionError, Redactor


def span(attrs=None, name="op", status="", events=None):
    return {
        "name": name,
        "status_message": status,
        "context": {"trace_id": "t", "span_id": "s"},
        "attributes": attrs or {},
        "events": events or [],
    }


class Detectors(unittest.TestCase):
    def setUp(self):
        self.r = Redactor()

    def text(self, value):
        return self.r.redact_span(span({"input.value": value}))["attributes"]["input.value"]

    def test_positives(self):
        cases = {
            "mail jane.doe+x@example.co.uk now": "mail [REDACTED:EMAIL] now",
            "call (415) 555-0132 today": "call [REDACTED:PHONE] today",
            "call 415-555-0132 today": "call [REDACTED:PHONE] today",
            "call +44 20 7946 0958 today": "call [REDACTED:PHONE] today",
            "ssn 123-45-6789 ok": "ssn [REDACTED:SSN] ok",
            "card 4111 1111 1111 1111 ok": "card [REDACTED:CREDIT_CARD] ok",
            "card 4111-1111-1111-1111": "card [REDACTED:CREDIT_CARD]",
            "from 192.168.1.20 to": "from [REDACTED:IP] to",
            "v6 2001:db8::ff00:42:8329 end": "v6 [REDACTED:IP] end",
            "key sk-ant-api03-abcdefghijklmnopqrstuvwx": "key [REDACTED:SECRET]",
            "aws AKIAIOSFODNN7EXAMPLE x": "aws [REDACTED:SECRET] x",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123": "Authorization: [REDACTED:SECRET]",
        }
        for given, want in cases.items():
            with self.subTest(given):
                self.assertEqual(self.text(given), want)

    def test_negatives(self):
        for value in [
            "timestamp 1792864644010",  # bare digits are not phones
            "order 4111111111111112",  # fails Luhn
            "ssn 000-12-3456 and 666-12-3456 and 900-12-3456",  # invalid areas
            "version 1.2.3 and time 12:30:45",
            "999.1.1.1",  # not an IP
            "a @ b and user@localhost",
            "2026-10-24T18:19:03.845437+00:00",
        ]:
            with self.subTest(value):
                self.assertEqual(self.text(value), value)

    def test_multiple_in_one_value(self):
        out = self.text("a@b.com and 415-555-0132 and 10.0.0.1")
        self.assertEqual(out, "[REDACTED:EMAIL] and [REDACTED:PHONE] and [REDACTED:IP]")

    def test_luhn(self):
        self.assertTrue(redact._luhn("4111111111111111"))
        self.assertFalse(redact._luhn("4111111111111112"))


class Scope(unittest.TestCase):
    def test_user_and_session_ids_left_alone_by_default(self):
        r = Redactor()
        out = r.redact_span(span({"user.id": "jane@example.com", "session.id": "415-555-0132"}))
        self.assertEqual(out["attributes"], {"user.id": "jane@example.com", "session.id": "415-555-0132"})

    def test_structural_attributes_untouched(self):
        a = {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4", "llm.token_count.prompt": 123,
             "input.mime_type": "text/plain", "tool.name": "lookup"}
        self.assertEqual(Redactor().redact_span(span(a))["attributes"], a)

    def test_non_strings_untouched(self):
        a = {"x.count": 4111111111111111, "x.flag": True, "x.none": None, "x.f": 1.5}
        self.assertEqual(Redactor().redact_span(span(a))["attributes"], a)

    def test_lists_and_nested(self):
        a = {"tags": ["a@b.com", "ok"], "obj": {"k": "415-555-0132"}}
        out = Redactor().redact_span(span(a))["attributes"]
        self.assertEqual(out, {"tags": ["[REDACTED:EMAIL]", "ok"], "obj": {"k": "[REDACTED:PHONE]"}})

    def test_name_status_and_events(self):
        out = Redactor().redact_span(
            span(name="lookup a@b.com", status="failed for 123-45-6789",
                 events=[{"name": "exception", "timestamp": "2026-01-01T00:00:00+00:00",
                          "attributes": {"exception.message": "bad ip 10.1.2.3"}}])
        )
        self.assertEqual(out["name"], "lookup [REDACTED:EMAIL]")
        self.assertEqual(out["status_message"], "failed for [REDACTED:SSN]")
        self.assertEqual(out["events"][0]["attributes"]["exception.message"], "bad ip [REDACTED:IP]")
        self.assertEqual(out["events"][0]["name"], "exception")

    def test_does_not_mutate_input(self):
        s = span({"input.value": "a@b.com"}, events=[{"name": "e", "timestamp": "t", "attributes": {"k": "a@b.com"}}])
        before = copy.deepcopy(s)
        Redactor().redact_span(s)
        self.assertEqual(s, before)

    def test_indexes_generalized_in_report(self):
        r = Redactor()
        r.redact_span(span({"llm.input_messages.0.message.content": "a@b.com",
                            "llm.input_messages.7.message.content": "c@d.com"}))
        self.assertEqual(r.counts[("email", "llm.input_messages.*.message.content")], 2)


class Rules(unittest.TestCase):
    def test_drop_mask_hash_keep(self):
        r = Redactor(salt="s", attributes={"metadata.email": "drop", "metadata.acct": "hash",
                                           "metadata.note": "mask", "input.value": "keep"})
        out = r.redact_span(span({"metadata.email": "x", "metadata.acct": "A-1", "metadata.note": "hi",
                                  "input.value": "a@b.com", "other": "ok"}))["attributes"]
        self.assertNotIn("metadata.email", out)
        self.assertRegex(out["metadata.acct"], r"^\[VALUE:[0-9a-f]{10}\]$")
        self.assertEqual(out["metadata.note"], "[REDACTED]")
        self.assertEqual(out["input.value"], "a@b.com")  # keep wins over detectors
        self.assertEqual(out["other"], "ok")

    def test_rule_overrides_default_skip(self):
        r = Redactor(salt="s", attributes={"user.id": "hash"})
        out = r.redact_span(span({"user.id": "jane"}))["attributes"]
        self.assertRegex(out["user.id"], r"^\[VALUE:[0-9a-f]{10}\]$")

    def test_hash_is_stable_and_salted(self):
        a = Redactor(action="hash", salt="one").redact_span(span({"k": "a@b.com"}))["attributes"]["k"]
        b = Redactor(action="hash", salt="one").redact_span(span({"k": "a@b.com"}))["attributes"]["k"]
        c = Redactor(action="hash", salt="two").redact_span(span({"k": "a@b.com"}))["attributes"]["k"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("[EMAIL:"))

    def test_custom_pattern_and_detector_subset(self):
        r = Redactor(detectors=[], patterns=[{"name": "employee_id", "regex": r"EMP-\d{6}"}])
        out = r.redact_span(span({"k": "EMP-123456 a@b.com"}))["attributes"]["k"]
        self.assertEqual(out, "[REDACTED:EMPLOYEE_ID] a@b.com")

    def test_extra_skip(self):
        r = Redactor(skip=["metadata.env"])
        self.assertEqual(r.redact_span(span({"metadata.env": "a@b.com"}))["attributes"]["metadata.env"], "a@b.com")

    def test_unsalted_hash_flag(self):
        self.assertTrue(Redactor(action="hash").unsalted_hash)
        self.assertFalse(Redactor(action="hash", salt="x").unsalted_hash)
        self.assertFalse(Redactor().unsalted_hash)
        self.assertTrue(Redactor(attributes={"k": "hash"}).unsalted_hash)


class Config(unittest.TestCase):
    def load(self, text):
        with TemporaryDirectory() as d:
            p = Path(d) / "c.yaml"
            p.write_text(text)
            return Redactor.from_config(p)

    def test_example_file_loads(self):
        r = Redactor.from_config(Path(__file__).parent / "redact.example.yaml")
        out = r.redact_span(span({"metadata.customer_email": "a@b.com", "x": "EMP-123456"}))["attributes"]
        self.assertEqual(out, {"x": "[REDACTED:EMPLOYEE_ID]"})

    def test_errors(self):
        for text in ["detectors: [nope]", "action: scramble", "bogus: 1", "patterns: [{name: a}]",
                     "patterns: [{name: a, regex: '('}]", "attributes: {k: explode}", "- a\n- b"]:
            with self.subTest(text), self.assertRaises(ConfigError):
                self.load(text)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            Redactor.from_config(Path("/nonexistent/redact.yaml"))

    def test_settings_id_changes_with_rules_and_salt(self):
        base = Redactor().settings_id
        self.assertEqual(base, Redactor().settings_id)
        self.assertNotEqual(base, Redactor(detectors=["email"]).settings_id)
        self.assertNotEqual(base, Redactor(attributes={"k": "drop"}).settings_id)
        self.assertNotEqual(Redactor(salt="a").settings_id, Redactor(salt="b").settings_id)


class FailClosed(unittest.TestCase):
    def test_error_raises_and_does_not_return_raw(self):
        r = Redactor()
        r._detectors = [("boom", type("P", (), {"sub": lambda *a, **k: 1 / 0})(), None)]
        with self.assertRaises(RedactionError):
            r.redact_span(span({"k": "a@b.com"}))

    def test_malformed_span_raises(self):
        with self.assertRaises(RedactionError):
            Redactor().redact_span({"attributes": "not a dict"})


if __name__ == "__main__":
    unittest.main()
