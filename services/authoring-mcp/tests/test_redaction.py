import json
from pathlib import Path

from pillarmesh_authoring_mcp.app import AuthoringApplication
from pillarmesh_evidence import PackageResult
from pillarmesh_evidence.package_models import CheckDisposition


def test_package_command_result_omits_local_destination_path(tmp_path: Path) -> None:
    local_canary = tmp_path / "operator-private-canary"
    result = PackageResult(
        path=local_canary,
        package_index_digest="a" * 64,
        verification_result_digest="b" * 64,
        checks=(CheckDisposition(name="package_structure"),),
    )

    rendered = json.dumps(AuthoringApplication._package_result(result))

    assert str(local_canary) not in rendered
    assert set(json.loads(rendered)) == {
        "checks",
        "package_index_digest",
        "verification_result_digest",
    }
