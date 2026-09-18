from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.ci.check_dco import main, unsigned_commits

_ISOLATED_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_ISOLATED_ENV,
    ).stdout.strip()


def _init(repo: Path) -> None:
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")


def _commit(repo: Path, name: str, message: str) -> str:
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
    signed = _commit(
        tmp_path,
        "b",
        "feat: signed\r\n\r\nSigned-off-by: karthik <karthik@pillarmesh.com>  \r\n",
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

    assert unsigned_commits(base, signed, cwd=tmp_path) == ()
    assert unsigned_commits(signed, wrong_name, cwd=tmp_path) == (wrong_name,)


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


def test_an_invalid_ref_makes_main_return_two_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["check_dco.py", base, "not-a-real-ref"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "not-a-real-ref" in captured.err or "invalid ref" in captured.err


def test_an_empty_range_where_base_equals_head_reports_no_missing_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    base = _commit(tmp_path, "a", "chore: base")

    assert unsigned_commits(base, base, cwd=tmp_path) == ()

    monkeypatch.chdir(tmp_path)
    assert main(["check_dco.py", base, base]) == 0
