"""Client-side PII redaction for spans, applied before anything is sent to Lumoz.

Best-effort pattern matching, not a compliance guarantee: it finds well-formed emails, phone
numbers, SSNs, card numbers, IP addresses and common API-key shapes, plus any regexes you add.
It does not find names or street addresses. Review a `--show-redactions` sample on your own data.

Rules, in order of precedence for each attribute:
  1. `attributes` in the config (glob on the attribute key): drop | mask | hash | keep
  2. built-in skip list (+ `skip` in the config): left untouched
  3. otherwise every string value is scanned with the enabled detectors
"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

SAMPLES_PER_DETECTOR = 3

# Attributes that are structure, not content. user.id and session.id are intentionally here:
# they are kept as-is unless the config sets a rule for them.
DEFAULT_SKIP = (
    "user.id",
    "session.id",
    "openinference.span.kind",
    "input.mime_type",
    "output.mime_type",
    "llm.model_name",
    "llm.provider",
    "llm.system",
    "llm.finish_reason",
    "tool.name",
    "*.token_count.*",
)


class RedactionError(Exception):
    """Redaction failed; the caller must stop rather than send the span unredacted."""


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _valid_card(text: str) -> bool:
    digits = re.sub(r"[ -]", "", text)
    return 13 <= len(digits) <= 19 and _luhn(digits)


def _valid_ssn(text: str) -> bool:
    area, group, serial = text.split("-")
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


def _valid_ipv6(text: str) -> bool:
    if text.count(":") < 2:
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

# (name, pattern, validator). Order matters: the placeholder text contains no digits or '@', so
# later detectors never match inside an earlier replacement.
BUILTIN_DETECTORS: list[tuple[str, re.Pattern[str], Optional[Callable[[str], bool]]]] = [
    (
        "secret",
        re.compile(
            r"\bsk-[A-Za-z0-9_-]{20,}"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bgh[pousr]_[A-Za-z0-9]{36,}"
            r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
            r"|\bAIza[0-9A-Za-z_-]{35}"
            r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            r"|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"
        ),
        None,
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), None),
    ("credit_card", re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?!\d)"), _valid_card),
    ("ssn", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), _valid_ssn),
    (
        "ip",
        re.compile(rf"(?<![\d.])(?:{_OCTET}\.){{3}}{_OCTET}(?!\d|\.\d)"),
        None,
    ),
    ("ip", re.compile(r"(?<![\w:])(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}(?![\w:])"), _valid_ipv6),
    # Only formats with a '+' country code or separators/parentheses: a bare run of digits is
    # far more often an ID or timestamp than a phone number.
    (
        "phone",
        re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\w)"),
        None,
    ),
    ("phone", re.compile(r"(?<![\w.])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)"), None),
]
BUILTIN_NAMES = tuple(dict.fromkeys(name for name, _, _ in BUILTIN_DETECTORS))
_ATTR_ACTIONS = ("drop", "mask", "hash", "keep")
_ALLOWED_CONFIG_KEYS = {"detectors", "patterns", "action", "salt", "attributes", "skip"}


def _generalize(key: str) -> str:
    """llm.input_messages.3.message.content -> llm.input_messages.*.message.content"""
    return re.sub(r"\.\d+(?=\.|$)", ".*", key)


# --------------------------------------------------------------------------- #
# Redactor
# --------------------------------------------------------------------------- #
class Redactor:
    def __init__(
        self,
        detectors: Optional[list[str]] = None,
        patterns: Optional[list[dict[str, str]]] = None,
        action: str = "mask",
        salt: str = "",
        attributes: Optional[dict[str, str]] = None,
        skip: Optional[list[str]] = None,
    ) -> None:
        names = list(BUILTIN_NAMES) if detectors is None else list(detectors)
        unknown = [n for n in names if n not in BUILTIN_NAMES]
        if unknown:
            raise ConfigError(f"unknown detector(s) {unknown}; valid: {', '.join(BUILTIN_NAMES)}")
        if action not in ("mask", "hash"):
            raise ConfigError("action must be 'mask' or 'hash'")
        self._detectors = [d for d in BUILTIN_DETECTORS if d[0] in names]
        for p in patterns or []:
            if not isinstance(p, dict) or not p.get("name") or not p.get("regex"):
                raise ConfigError("each pattern needs 'name' and 'regex'")
            try:
                self._detectors.append((str(p["name"]), re.compile(p["regex"]), None))
            except re.error as e:
                raise ConfigError(f"pattern '{p['name']}': invalid regex: {e}") from e
        self._action = action
        self._salt = salt
        self._rules = dict(attributes or {})
        bad = {k: v for k, v in self._rules.items() if v not in _ATTR_ACTIONS}
        if bad:
            raise ConfigError(f"attribute actions must be one of {_ATTR_ACTIONS}: {bad}")
        self._skip = list(DEFAULT_SKIP) + list(skip or [])
        self._settings = {
            "detectors": [d[0] for d in self._detectors],
            "regexes": [d[1].pattern for d in self._detectors],
            "action": action,
            "salt": hashlib.sha256(salt.encode()).hexdigest() if salt else "",
            "attributes": self._rules,
            "skip": self._skip,
        }
        self.counts: Counter[tuple[str, str]] = Counter()  # (detector or rule, attribute)
        self.samples: dict[str, list[tuple[str, str, str]]] = {}
        self.spans_touched = 0
        self.values_redacted = 0

    @classmethod
    def from_config(cls, path: Optional[Path]) -> "Redactor":
        if path is None:
            return cls(salt=os.environ.get("REDACT_SALT", ""))
        try:
            cfg = yaml.safe_load(Path(path).read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"cannot read {path}: {e}") from e
        if not isinstance(cfg, dict):
            raise ConfigError(f"{path}: expected a mapping at the top level")
        extra = set(cfg) - _ALLOWED_CONFIG_KEYS
        if extra:
            raise ConfigError(f"{path}: unknown key(s) {sorted(extra)}; allowed: {sorted(_ALLOWED_CONFIG_KEYS)}")
        return cls(
            detectors=cfg.get("detectors"),
            patterns=cfg.get("patterns"),
            action=cfg.get("action", "mask"),
            salt=str(cfg.get("salt") or os.environ.get("REDACT_SALT", "")),
            attributes=cfg.get("attributes"),
            skip=cfg.get("skip"),
        )

    @property
    def settings_id(self) -> str:
        """Short fingerprint of the active rules (the salt is hashed, never stored)."""
        return hashlib.sha256(json.dumps(self._settings, sort_keys=True).encode()).hexdigest()[:12]

    @property
    def unsalted_hash(self) -> bool:
        uses_hash = self._action == "hash" or "hash" in self._rules.values()
        return uses_hash and not self._salt

    # -- public ------------------------------------------------------------ #
    def redact_span(self, span: dict[str, Any]) -> dict[str, Any]:
        """Return a redacted copy of a Phoenix REST span; the input is not modified."""
        before = self.values_redacted
        try:
            out = dict(span)
            out["name"] = self._text(span.get("name") or "", "<span name>")
            out["status_message"] = self._text(span.get("status_message") or "", "<status message>")
            out["attributes"] = self._attributes(span.get("attributes") or {})
            out["events"] = [
                {**e, "attributes": self._attributes(e.get("attributes") or {}, prefix="event.")}
                for e in span.get("events", [])
            ]
        except Exception as e:  # fail closed: never fall back to the raw span
            raise RedactionError(f"span {span.get('context', {}).get('span_id')}: {e}") from e
        if self.values_redacted > before:
            self.spans_touched += 1
        return out

    def report(self, show_samples: bool = False) -> str:
        if not self.counts:
            return "No PII found."
        lines = [f"Redacted {self.values_redacted} item(s) in {self.spans_touched} span(s):"]
        for (what, attr), n in sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {n:>6}  {what:<12} {attr}")
        if show_samples:
            lines.append("Samples (these show the ORIGINAL text; do not share this output):")
            for what, items in sorted(self.samples.items()):
                for attr, original, replacement in items:
                    lines.append(f"  {what:<12} {attr}: {original!r} -> {replacement}")
        return "\n".join(lines)

    # -- internals --------------------------------------------------------- #
    def _rule_for(self, key: str) -> Optional[str]:
        for pattern, action in self._rules.items():
            if fnmatch.fnmatchcase(key, pattern):
                return action
        return None

    def _skipped(self, key: str) -> bool:
        return any(fnmatch.fnmatchcase(key, p) for p in self._skip)

    def _attributes(self, attrs: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in attrs.items():
            full = prefix + key
            rule = self._rule_for(key) or self._rule_for(full)
            if rule == "drop":
                self._count("drop", full)
                continue
            if rule in ("mask", "hash"):
                out[key] = self._replace(rule, "value", str(value))
                self._count(rule, full)
                continue
            if rule == "keep" or self._skipped(key):
                out[key] = value
                continue
            out[key] = self._value(value, full)
        return out

    def _value(self, value: Any, key: str) -> Any:
        if isinstance(value, str):
            return self._text(value, key)
        if isinstance(value, list):
            return [self._value(v, key) for v in value]
        if isinstance(value, dict):
            return {k: self._value(v, key) for k, v in value.items()}
        return value  # numbers, bools, None

    def _text(self, text: str, key: str) -> str:
        if not text:
            return text
        for name, pattern, valid in self._detectors:

            def sub(m: re.Match[str], name: str = name, valid: Optional[Callable[[str], bool]] = valid) -> str:
                if valid and not valid(m.group(0)):
                    return m.group(0)
                replacement = self._replace(self._action, name, m.group(0))
                self._count(name, key, m.group(0), replacement)
                return replacement

            text = pattern.sub(sub, text)
        return text

    def _replace(self, action: str, name: str, original: str) -> str:
        if action == "hash":
            digest = hashlib.sha256((self._salt + original).encode()).hexdigest()[:10]
            return f"[{name.upper()}:{digest}]"
        return f"[REDACTED:{name.upper()}]" if name != "value" else "[REDACTED]"

    def _count(self, what: str, key: str, original: str = "", replacement: str = "") -> None:
        self.counts[(what, _generalize(key))] += 1
        self.values_redacted += 1
        if original:
            bucket = self.samples.setdefault(what, [])
            if len(bucket) < SAMPLES_PER_DETECTOR:
                bucket.append((_generalize(key), original, replacement))
