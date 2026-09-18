from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from urllib.parse import quote

from .package_models import ScanFinding, ScanInput

_CONNECTION_STRING = re.compile(
    rb"(?i)(?:postgres(?:ql)?|snowflake|mysql|mongodb(?:\+srv)?|redis)://[^\s\"']+"
)
_PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:(?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
)
_FILE_URI = re.compile(rb"(?i)file://")
_ABSOLUTE_PATH = re.compile(
    rb"(?:/(?:Users|home|tmp|private/tmp|var/folders)/[^\s\"']+|[A-Za-z]:\\Users\\[^\s\"']+)"
)


def _variants(rule_prefix: str, value: str) -> Iterable[tuple[str, bytes]]:
    raw = value.encode("utf-8")
    candidates = (
        (f"{rule_prefix}_exact", raw),
        (f"{rule_prefix}_base64", base64.b64encode(raw)),
        (f"{rule_prefix}_url_encoded", quote(value, safe="").encode("ascii")),
    )
    seen: set[bytes] = set()
    for rule_id, encoded in candidates:
        if encoded not in seen:
            yield rule_id, encoded
            seen.add(encoded)


_HEXADECIMAL_DIGITS = frozenset(b"0123456789abcdefABCDEF")


def _is_hexadecimal(needle: bytes) -> bool:
    return all(byte in _HEXADECIMAL_DIGITS for byte in needle)


def _extends_a_hexadecimal_run(payload: bytes, offset: int, needle: bytes) -> bool:
    # An acceptance key is decimal and every decimal digit is a hexadecimal digit, so a short key
    # matches inside a digest by chance -- once in roughly twenty thousand exports at this package
    # size, which blocked a CI run and reads as an unexplained refusal. A digest is not an
    # appearance of the value: it is not derived from it and the value cannot be read back out of
    # it. Reporting a hexadecimal needle only where it is the whole hexadecimal run makes that
    # exact, rather than merely rarer, because a needle that lands at a digest boundary would
    # otherwise still match. The cost is a value abutting hexadecimal characters on one side, such
    # as `abc984201`; the exporter writes canonical JSON, where a leaked key is a number or a
    # string value and is therefore delimited.
    before = payload[offset - 1 : offset] if offset else b""
    after = payload[offset + len(needle) : offset + len(needle) + 1]
    return any(byte in _HEXADECIMAL_DIGITS for byte in before + after)


def _offsets(payload: bytes, needle: bytes) -> Iterable[int]:
    offset = payload.find(needle)
    while offset >= 0:
        yield offset
        offset = payload.find(needle, offset + 1)


def scan_bytes(
    relative_path: str, payload: bytes, scan_input: ScanInput
) -> tuple[ScanFinding, ...]:
    del relative_path
    findings: list[ScanFinding] = []
    values: list[tuple[str, str]] = []
    values.extend(("credential_canary", value) for value in scan_input.credential_canaries)
    values.extend(("row_value_canary", value) for value in scan_input.row_value_canaries)
    values.extend(("acceptance_key", str(value)) for value in scan_input.acceptance_keys)
    values.extend(("local_path_prefix", value) for value in scan_input.local_path_prefixes)
    for prefix, value in values:
        seen: set[tuple[str, int]] = set()
        for rule_id, needle in _variants(prefix, value):
            hexadecimal = _is_hexadecimal(needle)
            for offset in _offsets(payload, needle):
                if hexadecimal and _extends_a_hexadecimal_run(payload, offset, needle):
                    continue
                key = (rule_id, offset)
                if key not in seen:
                    findings.append(
                        ScanFinding(
                            rule_id=rule_id,
                            byte_offset=offset,
                        )
                    )
                    seen.add(key)
    for rule_id, pattern in (
        ("connection_string", _CONNECTION_STRING),
        ("private_key_marker", _PRIVATE_KEY),
        ("file_uri", _FILE_URI),
        ("local_absolute_path", _ABSOLUTE_PATH),
    ):
        findings.extend(
            ScanFinding(
                rule_id=rule_id,
                byte_offset=match.start(),
            )
            for match in pattern.finditer(payload)
        )
    return tuple(sorted(findings, key=lambda item: (item.byte_offset, item.rule_id)))
