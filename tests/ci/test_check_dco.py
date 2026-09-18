from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.ci.check_dco import SHALLOW_HINT, DcoCheckError, main, unsigned_commits


@pytest.fixture(autouse=True)
def _sanitized_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the git-facing test environment from scratch.

    Every test, and the checker's own subprocess calls (which inherit the process
    environment), run against PATH plus a `HOME` scoped to this test's `tmp_path`,
    with no global/system git config and no ambient `GIT_*` variable that a
    developer's shell might have set (author identity, alternate work-tree,
    pager, etc.) -- so a passing test proves the checker's own behavior rather
    than an accident of whoever's machine or CI runner it happened to run on.
    """
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _init(repo: Path) -> None:
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")


def _commit(repo: Path, name: str, message: str, *, cleanup: str = "strip") -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(
        repo,
        "-c",
        "user.name=karthik",
        "-c",
        "user.email=karthik@pillarmesh.com",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "commit",
        "-q",
        f"--cleanup={cleanup}",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_only_commits_without_a_matching_sign_off_are_reported(tmp_path: Path) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    signed = _commit(
        tmp_path, "b", "feat: signed\n\nSigned-off-by: karthik <karthik@pillarmesh.com>"
    )
    unsigned = _commit(tmp_path, "c", "fix: unsigned")
    wrong = _commit(tmp_path, "d", "docs: other\n\nSigned-off-by: someone <someone@example.com>")

    assert unsigned_commits(base, wrong, cwd=tmp_path) == (unsigned, wrong)
    assert signed not in unsigned_commits(base, wrong, cwd=tmp_path)


def test_trailing_whitespace_and_crlf_on_the_sign_off_line_still_count(tmp_path: Path) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    # --cleanup=verbatim stops git from stripping the trailing whitespace itself,
    # so this proves *our* stripping (not git's default message cleanup) is what
    # makes a whitespace-padded, CRLF sign-off line still count.
    signed = _commit(
        tmp_path,
        "b",
        "feat: signed\r\n\r\nSigned-off-by: karthik <karthik@pillarmesh.com>  \r\n",
        cleanup="verbatim",
    )

    assert unsigned_commits(base, signed, cwd=tmp_path) == ()


def test_sign_off_email_matches_case_insensitively_but_name_matches_exactly(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    signed = _commit(
        tmp_path,
        "b",
        "feat: signed\n\nSigned-off-by: karthik <Karthik@PillarMesh.com>",
    )
    wrong_name = _commit(
        tmp_path,
        "c",
        "fix: wrong name\n\nSigned-off-by: Someone Else <karthik@pillarmesh.com>",
    )
    wrong_case_name = _commit(
        tmp_path,
        "e",
        "fix: wrong name case\n\nSigned-off-by: Karthik <karthik@pillarmesh.com>",
    )

    assert unsigned_commits(base, signed, cwd=tmp_path) == ()
    assert unsigned_commits(signed, wrong_name, cwd=tmp_path) == (wrong_name,)
    assert unsigned_commits(wrong_name, wrong_case_name, cwd=tmp_path) == (wrong_case_name,)


def test_a_sign_off_quoted_mid_body_before_trailing_prose_does_not_count(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    quoted_mid_body = _commit(
        tmp_path,
        "b",
        "fix: quoted sign-off\n\n"
        "Signed-off-by: karthik <karthik@pillarmesh.com>\n\n"
        "This paragraph of prose comes after the quoted line above, so it is not\n"
        "a trailer block and must not satisfy the check.",
    )

    assert unsigned_commits(base, quoted_mid_body, cwd=tmp_path) == (quoted_mid_body,)


def test_merge_commits_in_the_range_are_checked_like_any_other_commit(tmp_path: Path) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    _git(tmp_path, "branch", "side")
    main_tip = _commit(
        tmp_path, "b", "feat: main\n\nSigned-off-by: karthik <karthik@pillarmesh.com>"
    )
    _git(tmp_path, "checkout", "-q", "side")
    _commit(tmp_path, "c", "feat: side\n\nSigned-off-by: karthik <karthik@pillarmesh.com>")
    _git(tmp_path, "checkout", "-q", "-B", "main", main_tip)
    _git(
        tmp_path,
        "-c",
        "user.name=karthik",
        "-c",
        "user.email=karthik@pillarmesh.com",
        "-c",
        "commit.gpgsign=false",
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "merge: combine branches",
        "side",
    )
    merge_sha = _git(tmp_path, "rev-parse", "HEAD")

    missing = unsigned_commits(base, merge_sha, cwd=tmp_path)

    assert merge_sha in missing


def _commit_with_author_name(repo: Path, name: str, author_name: str, message: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(
        repo,
        "-c",
        f"user.name={author_name}",
        "-c",
        "user.email=attacker@example.com",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "commit",
        "-q",
        "--cleanup=strip",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_a_field_separator_byte_inside_the_author_name_does_not_shift_fields(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    # An author name carrying the OLD field-separator byte used to shift every
    # field after it by one, so the sign-off's genuine name/email pair ended up
    # compared against the wrong slice of the record and matched by accident.
    spoofed = _commit_with_author_name(
        tmp_path,
        "b",
        "Alice\x1falice@x",
        "feat: spoofed\n\nSigned-off-by: Alice <alice@x>",
    )

    assert unsigned_commits(base, spoofed, cwd=tmp_path) == (spoofed,)


def test_an_embedded_record_separator_byte_in_the_author_name_is_handled_cleanly(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    weird = _commit_with_author_name(
        tmp_path,
        "b",
        "Bob\x1eEve",
        "feat: no sign-off",
    )

    try:
        result = unsigned_commits(base, weird, cwd=tmp_path)
    except DcoCheckError:
        return
    assert weird in result


def test_showsignature_local_config_does_not_break_the_check(tmp_path: Path) -> None:
    _init(tmp_path)
    _git(tmp_path, "config", "log.showSignature", "true")
    base = _commit(tmp_path, "a", "chore: base")
    signed = _commit(
        tmp_path, "b", "feat: signed\n\nSigned-off-by: karthik <karthik@pillarmesh.com>"
    )

    assert unsigned_commits(base, signed, cwd=tmp_path) == ()


def test_an_invalid_ref_makes_main_return_two_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["check_dco.py", base, "not-a-real-ref"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid ref for HEAD" in captured.err
    assert "not-a-real-ref" in captured.err


def test_an_empty_base_ref_makes_main_return_two_naming_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["check_dco.py", "", "HEAD"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid ref for BASE" in captured.err


def test_a_ref_starting_with_a_dash_is_rejected_before_reaching_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["check_dco.py", base, "-n"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid ref for HEAD" in captured.err
    assert "-n" in captured.err


def test_an_output_flag_ref_is_rejected_and_creates_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)
    output_target = tmp_path / "x"
    malicious_ref = f"--output={output_target}"

    exit_code = main(["check_dco.py", base, malicious_ref])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid ref for HEAD" in captured.err
    assert not output_target.exists()


def test_a_tree_ref_is_rejected_rather_than_treated_as_a_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["check_dco.py", base, "HEAD^{tree}"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid ref for HEAD" in captured.err


def test_a_shallow_clone_is_rejected_with_a_fetch_depth_hint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _init(source)
    base = _commit(source, "a", "chore: base")
    _commit(source, "b", "feat: signed\n\nSigned-off-by: karthik <karthik@pillarmesh.com>")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--depth=1", source.as_uri(), str(clone))

    with pytest.raises(Exception) as excinfo:
        unsigned_commits(base, "HEAD", cwd=clone)
    assert SHALLOW_HINT in str(excinfo.value)


def test_an_empty_range_where_base_equals_head_reports_no_missing_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")

    assert unsigned_commits(base, base, cwd=tmp_path) == ()

    monkeypatch.chdir(tmp_path)
    assert main(["check_dco.py", base, base]) == 0


def test_a_missing_git_binary_makes_main_return_two_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    exit_code = main(["check_dco.py", base, "HEAD"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "git" in captured.err.lower()
