"""Tests for quality_pipeline.git_ops — git helper functions."""

from __future__ import annotations

import subprocess
from pathlib import Path
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


class TestGitRollbackPreservesPreExisting:
    def test_does_not_remove_pre_existing_untracked(self, tmp_path, monkeypatch):
        """Pre-existing untracked files should NOT be removed during rollback."""
        pre_existing = tmp_path / "pre_existing.py"
        pre_existing.write_text("keep me")
        new_file = tmp_path / "new.py"
        new_file.write_text("delete me")

        def mock_git(*args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            # ls-files returns both files as untracked
            if "ls-files" in args:
                r.stdout = f"{pre_existing}\n{new_file}\n"
            else:
                r.stdout = ""
            return r

        monkeypatch.setattr(qp.git_ops, "git", mock_git)
        qp.git_rollback_round({str(pre_existing)})
        # pre-existing should still exist; new file should be removed
        assert pre_existing.exists()
        assert not new_file.exists()


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


class TestSetupWorktree:
    @staticmethod
    def _setup_worktree_mocks(monkeypatch, calls, wt_path, branch_exists=False):
        """Set up mocks for setup_worktree tests.

        Creates wt_path so mkdtemp/rmdir cycle works, and mocks git to
        recreate it on 'worktree add' (simulating real git behavior).
        """
        def mock_git(*args, **kwargs):
            calls.append(args)
            r = MagicMock()
            if "show-ref" in args:
                r.returncode = 0 if branch_exists else 1
            else:
                r.returncode = 0
            if "worktree" in args and "add" in args:
                wt_path.mkdir(exist_ok=True)
            return r

        monkeypatch.setattr(qp.git_ops, "git", mock_git)

        # mkdtemp must create the dir (like the real one does) so rmdir() works
        def mock_mkdtemp(prefix=""):
            wt_path.mkdir(exist_ok=True)
            return str(wt_path)

        monkeypatch.setattr("tempfile.mkdtemp", mock_mkdtemp)

    def test_new_branch_calls_worktree_add_b(self, tmp_path, monkeypatch):
        """When branch doesn't exist, use 'git worktree add -b'."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        monkeypatch.chdir(orig_dir)

        calls = []
        wt_path = tmp_path / "worktree-dir"
        self._setup_worktree_mocks(monkeypatch, calls, wt_path, branch_exists=False)

        wt_dir, original_dir = qp.setup_worktree("quality/test", [])
        assert original_dir == orig_dir
        wt_calls = [c for c in calls if "worktree" in c]
        assert any("-b" in c for c in wt_calls)

    def test_existing_branch_calls_worktree_add_without_b(self, tmp_path, monkeypatch):
        """When branch exists, use 'git worktree add' without -b."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        monkeypatch.chdir(orig_dir)

        calls = []
        wt_path = tmp_path / "worktree-dir"
        self._setup_worktree_mocks(monkeypatch, calls, wt_path, branch_exists=True)

        qp.setup_worktree("quality/test", [])
        wt_calls = [c for c in calls if "worktree" in c]
        assert not any("-b" in c for c in wt_calls)

    def test_symlinks_dirs(self, tmp_path, monkeypatch):
        """Existing dirs in original should be symlinked into worktree."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        (orig_dir / "node_modules").mkdir()
        monkeypatch.chdir(orig_dir)

        calls = []
        wt_path = tmp_path / "worktree-dir"
        self._setup_worktree_mocks(monkeypatch, calls, wt_path)

        qp.setup_worktree("quality/test", ["node_modules"])
        link = wt_path / "node_modules"
        assert link.is_symlink()
        assert link.resolve() == (orig_dir / "node_modules").resolve()

    def test_symlinks_env_files(self, tmp_path, monkeypatch):
        """Existing .env files should be symlinked."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        (orig_dir / ".env").write_text("SECRET=val")
        monkeypatch.chdir(orig_dir)

        calls = []
        wt_path = tmp_path / "worktree-dir"
        self._setup_worktree_mocks(monkeypatch, calls, wt_path)

        qp.setup_worktree("quality/test", [])
        link = wt_path / ".env"
        assert link.is_symlink()

    def test_changes_cwd(self, tmp_path, monkeypatch):
        """setup_worktree should chdir to the worktree."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        monkeypatch.chdir(orig_dir)

        calls = []
        wt_path = tmp_path / "worktree-dir"
        self._setup_worktree_mocks(monkeypatch, calls, wt_path)

        qp.setup_worktree("quality/test", [])
        assert Path.cwd() == wt_path
