import base64
from urllib.parse import quote

import pytest
from pillarmesh_evidence import ScanInput, scan_bytes


@pytest.mark.parametrize(
    ("variant", "rule_id"),
    [
        (lambda value: value, "credential_canary_exact"),
        (
            lambda value: base64.b64encode(value.encode("utf-8")).decode("ascii"),
            "credential_canary_base64",
        ),
        (lambda value: quote(value, safe=""), "credential_canary_url_encoded"),
    ],
)
def test_scanner_finds_canary_forms_without_echoing_source_value(
    variant: object, rule_id: str
) -> None:
    canary = "scan-secret:p@ss/word"
    encoded = variant(canary)  # type: ignore[operator]

    findings = scan_bytes(
        "trace/events.json",
        f"prefix:{encoded}:suffix".encode(),
        ScanInput(credential_canaries=(canary,)),
    )

    assert [finding.rule_id for finding in findings] == [rule_id]
    assert findings[0].byte_offset == len("prefix:")
    assert canary not in repr(findings)


@pytest.mark.parametrize(
    ("payload", "scan_input", "rule_id"),
    [
        (b'"order_id":984201', ScanInput(acceptance_keys=(984201,)), "acceptance_key_exact"),
        (
            b'"customer_ref":"customer-sensitive-17"',
            ScanInput(row_value_canaries=("customer-sensitive-17",)),
            "row_value_canary_exact",
        ),
        (
            b'"path":"/Users/operator/private/segment.csv"',
            ScanInput(local_path_prefixes=("/Users/operator/private",)),
            "local_path_prefix_exact",
        ),
        (b'"path":"file:///tmp/private/segment.csv"', ScanInput(), "file_uri"),
        (
            b'"dsn":"postgresql://runtime:password@db.invalid/m0"',
            ScanInput(),
            "connection_string",
        ),
        (
            b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key",
            ScanInput(),
            "private_key_marker",
        ),
        (
            b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nnot-a-real-key",
            ScanInput(),
            "private_key_marker",
        ),
    ],
)
def test_scanner_fails_closed_for_private_material(
    payload: bytes, scan_input: ScanInput, rule_id: str
) -> None:
    findings = scan_bytes("payload.json", payload, scan_input)

    assert rule_id in {finding.rule_id for finding in findings}


def test_scanner_reports_all_offsets_without_returning_matched_bytes() -> None:
    findings = scan_bytes(
        "payload.json",
        b"safe secret safe secret",
        ScanInput(credential_canaries=("secret",)),
    )

    assert [(item.rule_id, item.byte_offset) for item in findings] == [
        ("credential_canary_exact", 5),
        ("credential_canary_exact", 17),
    ]
    assert all(not hasattr(item, "matched_value") for item in findings)


def test_scanner_cannot_echo_canary_through_caller_controlled_path() -> None:
    canary = "caller-controlled-sensitive-value"

    findings = scan_bytes(
        canary,
        canary.encode("utf-8"),
        ScanInput(credential_canaries=(canary,)),
    )

    assert findings
    assert canary not in repr(findings)
