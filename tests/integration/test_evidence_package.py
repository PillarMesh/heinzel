from pathlib import Path

from pillarmesh_evidence import export_package, verify_package

from services.evidence.tests.test_package import RUN_ID, _metadata, complete_store


def test_complete_fake_run_exports_and_verifies_across_component_boundaries(
    tmp_path: Path,
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)

    exported = export_package(
        store,
        RUN_ID,
        tmp_path / "evidence-package",
        _metadata(),
        scan_input,
        verifier,
    )
    independently_verified = verify_package(exported.path, verifier, scan_input)

    assert independently_verified == exported
    assert (exported.path / "package.json").is_file()
    assert (exported.path / "verification/result.json").is_file()
