"""Tests for quality_pipeline.git_ops — git helper functions."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

import quality_pipeline as qp
from conftest import _mock_git_fn


class TestGitHasUncommitted:
    def test_clean_repo(self, monkeypatch):
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(returncode=0))
        assert qp.git_has_uncommitted() is False

    def test_unstaged_changes(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            # First call (git diff --quiet) fails, second succeeds
            r.returncode = 1 if len(calls) == 1 else 0
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        assert qp.git_has_uncommitted() is True

    def test_staged_changes(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            # First call succeeds, second (--cached) fails
            r.returncode = 0 if len(calls) == 1 else 1
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        assert qp.git_has_uncommitted() is True


class TestGitUntrackedFiles:
    def test_no_untracked(self, monkeypatch):
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=""))
        assert qp.git_untracked_files() == set()

    def test_some_untracked(self, monkeypatch):
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout="a.py\nb.py\n"))
        assert qp.git_untracked_files() == {"a.py", "b.py"}

    def test_whitespace_only(self, monkeypatch):
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout="  \n"))
        assert qp.git_untracked_files() == set()


class TestGitStageRoundChanges:
    def test_stages_modified_and_new(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = "new_file.py\n" if "ls-files" in args else ""
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_stage_round_changes(set())
        # Should have: add -u, ls-files, add -- new_file.py
        add_calls = [c for c in calls if c[0] == "add"]
        assert any("-u" in c for c in add_calls)
        assert any("new_file.py" in c for c in add_calls)

    def test_does_not_stage_preexisting_untracked(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = "old.py\nnew.py\n" if "ls-files" in args else ""
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_stage_round_changes({"old.py"})
        # Only new.py should be added, not old.py
        add_file_calls = [c for c in calls if len(c) >= 3 and c[0] == "add" and c[1] == "--"]
        assert len(add_file_calls) == 1
        assert add_file_calls[0][2] == "new.py"


class TestGitRollbackRound:
    def test_resets_and_removes_new_files(self, tmp_path, monkeypatch):
        new_file = tmp_path / "new.py"
        new_file.write_text("content")
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = str(new_file) + "\n" if "ls-files" in args else ""
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_rollback_round(set())
        # Should call reset and checkout
        assert any("reset" in c for c in calls)
        assert any("checkout" in c for c in calls)


class TestGitCreateBranch:
    def test_creates_new_branch(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            # show-ref fails (branch doesn't exist)
            r.returncode = 1 if "show-ref" in args else 0
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_create_branch("quality/test")
        assert any("checkout" in c and "-b" in c for c in calls)

    def test_uses_existing_branch(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.returncode = 0
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_create_branch("quality/test")
        # Should checkout without -b
        checkout_calls = [c for c in calls if "checkout" in c]
        assert any("-b" not in c for c in checkout_calls)


class TestGitCommit:
    def test_success_with_no_gpg_sign(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.returncode = 0
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_commit("test msg")
        assert len(calls) == 1
        assert "--no-gpg-sign" in calls[0]

    def test_gpg_fallback(self, monkeypatch):
        calls = []
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            if len(calls) == 1:
                r.returncode = 1
                r.stderr = "error: gpg failed to sign"
            else:
                r.returncode = 0
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_commit("test msg")
        assert len(calls) == 2
        assert "--no-gpg-sign" not in calls[1]

    def test_non_gpg_failure_raises(self, monkeypatch):
        def mock_git(*args, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "error: pathspec not found"
            return r
        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        with pytest.raises(subprocess.CalledProcessError):
            qp.git_commit("test msg")


class TestGitAcquireLock:
    def test_dry_run_returns_none(self):
        assert qp.git_acquire_lock(True) is None

    def test_creates_lock_dir(self, tmp_path, monkeypatch):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))
        result = qp.git_acquire_lock(False)
        assert result is not None
        expected = git_dir / "quality-pipeline.lock"
        assert result == expected
        assert result.is_dir()
        # Cleanup
        result.rmdir()

    def test_concurrent_lock_exits(self, tmp_path, monkeypatch):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "quality-pipeline.lock").mkdir()
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout=str(git_dir) + "\n"))
        with pytest.raises(SystemExit):
            qp.git_acquire_lock(False)


class TestGitRevParseHead:
    def test_returns_sha(self, monkeypatch):
        monkeypatch.setattr(qp.git_ops, "git", _mock_git_fn(stdout="abc123\n"))
        assert qp.git_rev_parse_head() == "abc123"
