"""Tests for quality_pipeline.pipeline — run_round, pipeline orchestrator."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock

import pytest
import yaml

import quality_pipeline as qp
from conftest import _mock_git_fn


class TestRunRound:
    """Tests for run_round with mocked externals."""

    @pytest.fixture
    def round_file(self, tmp_path):
        f = tmp_path / "01-test.md"
        f.write_text("---\nname: test-round\ngate: hard\n---\nDo stuff.\n")
        return f

    @pytest.fixture
    def log_dir(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        return d

    @pytest.fixture
    def mock_env(self, monkeypatch):
        """Set up common mocks for run_round tests."""
        monkeypatch.setattr(qp.pipeline_mod, "git_rev_parse_head", lambda: "abc123")
        monkeypatch.setattr(qp.pipeline_mod, "git_untracked_files", lambda: set())
        monkeypatch.setattr(
            qp.pipeline_mod, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )
        monkeypatch.setattr(
            qp.pipeline_mod, "run_static_analysis", lambda *a, **kw: ""
        )
        monkeypatch.setattr(qp.pipeline_mod, "git_stage_round_changes", lambda pre: None)
        monkeypatch.setattr(qp.pipeline_mod, "git_commit", lambda msg: None)
        monkeypatch.setattr(qp.pipeline_mod, "run_reviewer", lambda *a, **kw: None)
        # Prevent resource monitor from actually running
        monkeypatch.setattr(
            qp.ResourceMonitor, "start", lambda self: None,
        )
        monkeypatch.setattr(
            qp.ResourceMonitor, "stop", lambda self: None,
        )

    def test_claude_failure_hard_gate(
        self, round_file, log_dir, mock_env, monkeypatch
    ):
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 1)
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.HARD_FAILED

    def test_claude_failure_soft_gate(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-soft.md"
        f.write_text("---\nname: soft-round\ngate: soft\n---\nDo stuff.\n")
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 1)
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.SOFT_FAILED

    def test_no_changes(self, round_file, log_dir, mock_env, monkeypatch):
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        # git diff --quiet returns 0 (no changes)
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=0))
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.NO_CHANGES

    def test_gate_none_skips_tests(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-none.md"
        f.write_text("---\nname: none-round\ngate: none\n---\nDo stuff.\n")
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        # Simulate changes exist
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=1))
        test_calls = []
        monkeypatch.setattr(
            qp.pipeline_mod, "run_tests_with_tee",
            lambda *a, **kw: test_calls.append(1) or 0,
        )
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.PASSED
        assert len(test_calls) == 0  # tests should not have been run

    def test_tests_pass(self, round_file, log_dir, mock_env, monkeypatch):
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(qp.pipeline_mod, "run_tests_with_tee", lambda *a, **kw: 0)
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.PASSED

    def test_tests_fail_no_retries(
        self, round_file, log_dir, mock_env, monkeypatch
    ):
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(qp.pipeline_mod, "run_tests_with_tee", lambda *a, **kw: 1)
        monkeypatch.setattr(qp.pipeline_mod, "git_rollback_round", lambda pre: None)
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.HARD_FAILED

    def test_tests_fail_then_retry_succeeds(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-retry.md"
        f.write_text(
            "---\nname: retry-round\ngate: hard\nmax_retries: 1\n---\nDo stuff.\n"
        )
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=1))
        test_attempts = []
        def mock_tests(cmd, output_file, **kw):
            test_attempts.append(1)
            # Write something so retry can read it
            output_file.write_text("FAIL: test_foo")
            return 1 if len(test_attempts) == 1 else 0
        monkeypatch.setattr(qp.pipeline_mod, "run_tests_with_tee", mock_tests)
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.PASSED
        assert len(test_attempts) == 2

    def test_tests_fail_soft_gate_rollback(
        self, tmp_path, log_dir, mock_env, monkeypatch
    ):
        f = tmp_path / "01-soft.md"
        f.write_text("---\nname: soft-fail\ngate: soft\n---\nDo stuff.\n")
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(qp.pipeline_mod, "run_tests_with_tee", lambda *a, **kw: 1)
        rolled_back = []
        monkeypatch.setattr(
            qp.pipeline_mod, "git_rollback_round", lambda pre: rolled_back.append(1)
        )
        result = qp.run_round(
            f, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.SOFT_FAILED
        assert len(rolled_back) == 1


class TestRemainingSeconds:
    def test_time_remaining(self):
        start = time.time() - 120  # 2 minutes ago
        result = qp.pipeline_mod._remaining_seconds(start, 5)
        # Should be ~3 minutes = ~180 seconds
        assert 170 <= result <= 190

    def test_budget_exhausted(self):
        start = time.time() - 600  # 10 minutes ago
        result = qp.pipeline_mod._remaining_seconds(start, 5)
        assert result < 0


class TestRunRoundReviewerVerdict:
    """Test that reviewer critical verdict downgrades run_round outcome."""

    @pytest.fixture
    def round_file(self, tmp_path):
        f = tmp_path / "01-test.md"
        f.write_text(
            "---\nname: test-round\ngate: none\nreview_gate: hard\n---\nDo stuff.\n"
        )
        return f

    @pytest.fixture
    def log_dir(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        return d

    @pytest.fixture
    def mock_env(self, monkeypatch):
        monkeypatch.setattr(qp.pipeline_mod, "git_rev_parse_head", lambda: "abc123")
        monkeypatch.setattr(qp.pipeline_mod, "git_untracked_files", lambda: set())
        monkeypatch.setattr(
            qp.pipeline_mod, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )
        monkeypatch.setattr(
            qp.pipeline_mod, "run_static_analysis", lambda *a, **kw: ""
        )
        monkeypatch.setattr(qp.pipeline_mod, "git_stage_round_changes", lambda pre: None)
        monkeypatch.setattr(qp.pipeline_mod, "git_commit", lambda msg: None)
        monkeypatch.setattr(qp.ResourceMonitor, "start", lambda self: None)
        monkeypatch.setattr(qp.ResourceMonitor, "stop", lambda self: None)

    def test_critical_verdict_hard_gate_fails_round(
        self, round_file, log_dir, mock_env, monkeypatch
    ):
        monkeypatch.setattr(qp.pipeline_mod, "run_claude", lambda *a, **kw: 0)
        # Simulate changes exist (gate=none skips tests, goes straight to commit+review)
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(returncode=1))
        monkeypatch.setattr(
            qp.pipeline_mod, "run_reviewer", lambda *a, **kw: "critical"
        )
        result = qp.run_round(
            round_file, 1, 1, "true", qp.PipelineConfig(), None, log_dir, "none"
        )
        assert result == qp.RoundOutcome.HARD_FAILED


class TestCheckReviewVerdict:
    def _rc(self, review_gate="none"):
        return qp.RoundConfig(name="test", review_gate=review_gate)

    def test_none_verdict_passes(self):
        assert qp._check_review_verdict(None, self._rc()) == qp.RoundOutcome.PASSED

    def test_pass_verdict_passes(self):
        assert qp._check_review_verdict("pass", self._rc()) == qp.RoundOutcome.PASSED

    def test_warn_verdict_passes(self):
        assert qp._check_review_verdict("warn", self._rc()) == qp.RoundOutcome.PASSED

    def test_critical_gate_none_passes(self):
        assert qp._check_review_verdict("critical", self._rc("none")) == qp.RoundOutcome.PASSED

    def test_critical_gate_hard_fails(self):
        assert qp._check_review_verdict("critical", self._rc("hard")) == qp.RoundOutcome.HARD_FAILED

    def test_critical_gate_soft_fails(self):
        assert qp._check_review_verdict("critical", self._rc("soft")) == qp.RoundOutcome.SOFT_FAILED


class TestPipeline:
    """Tests for the pipeline() orchestrator with mocked dependencies."""

    @pytest.fixture
    def pipeline_env(self, tmp_path, monkeypatch):
        """Set up a minimal environment for pipeline tests."""
        # Create round files
        rounds_dir = tmp_path / "rounds"
        rounds_dir.mkdir()
        (rounds_dir / "01-audit.md").write_text(
            "---\nname: audit\ngate: hard\n---\nAudit stuff.\n"
        )
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", rounds_dir)
        # pipeline.py imports ROUNDS_DIR and friends from config at function level,
        # but discover_rounds/resolve_round_file read config.ROUNDS_DIR at call time.
        # We also patch the names imported into pipeline.py for full coverage.
        monkeypatch.setattr(qp.pipeline_mod, "discover_rounds", qp.discover_rounds)
        monkeypatch.setattr(qp.pipeline_mod, "resolve_round_file", qp.resolve_round_file)

        # We're in a git repo (mock subprocess for git check)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""),
        )
        # Mock git helper
        monkeypatch.setattr(qp.pipeline_mod, "git", _mock_git_fn(stdout="abc1234\n"))
        monkeypatch.setattr(qp.pipeline_mod, "git_acquire_lock", lambda dry_run: None)
        monkeypatch.setattr(qp.pipeline_mod, "git_has_uncommitted", lambda: False)
        monkeypatch.setattr(qp.pipeline_mod, "git_create_branch", lambda name: None)
        monkeypatch.setattr(qp.pipeline_mod, "detect_gpu", lambda: "none")
        monkeypatch.setattr(
            qp.pipeline_mod, "get_resource_snapshot", lambda gpu_type="none": "CPU: ok"
        )

        # Change to tmp_path so pipeline doesn't operate on real repo
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_dry_run(self, pipeline_env, monkeypatch):
        """Dry run should not call run_round."""
        run_round_calls = []
        monkeypatch.setattr(
            qp.pipeline_mod, "run_round",
            lambda *a, **kw: run_round_calls.append(1) or qp.RoundOutcome.PASSED,
        )
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=1,
            dry_run=True,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        assert len(run_round_calls) == 0

    def test_single_round_passes(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(
            qp.pipeline_mod, "run_round",
            lambda *a, **kw: qp.RoundOutcome.PASSED,
        )
        # Should not raise or sys.exit
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=1,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )

    def test_hard_failure_exits_nonzero(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(
            qp.pipeline_mod, "run_round",
            lambda *a, **kw: qp.RoundOutcome.HARD_FAILED,
        )
        with pytest.raises(SystemExit) as exc_info:
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )
        assert exc_info.value.code == 1

    def test_start_from_skips_rounds(self, tmp_path, pipeline_env, monkeypatch):
        rounds_dir = tmp_path / "rounds"
        (rounds_dir / "02-refactor.md").write_text(
            "---\nname: refactor\ngate: hard\n---\nRefactor stuff.\n"
        )
        round_names = []
        def mock_run_round(rf, n, total, *args, **kwargs):
            rc = qp.parse_frontmatter(rf)
            round_names.append(rc.name)
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp.pipeline_mod, "run_round", mock_run_round)
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=2,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        # Only the second round should have been run
        assert round_names == ["refactor"]

    def test_no_test_command_auto_detect_fails(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(qp.pipeline_mod, "detect_test_command", lambda path: None)
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command=None,
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_no_rounds_found_exits(self, tmp_path, pipeline_env, monkeypatch):
        # Empty rounds dir
        empty_rounds = tmp_path / "empty_rounds"
        empty_rounds.mkdir()
        monkeypatch.setattr(qp.config, "ROUNDS_DIR", empty_rounds)
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_unknown_round_name_exits(self, pipeline_env, monkeypatch):
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg="nonexistent-round",
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_start_from_exceeds_total(self, pipeline_env, monkeypatch):
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=99,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_soft_failure_continues(self, tmp_path, pipeline_env, monkeypatch):
        rounds_dir = tmp_path / "rounds"
        (rounds_dir / "02-refactor.md").write_text(
            "---\nname: refactor\ngate: hard\n---\nRefactor stuff.\n"
        )
        call_count = [0]
        def mock_run_round(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return qp.RoundOutcome.SOFT_FAILED
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp.pipeline_mod, "run_round", mock_run_round)
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=None,
            start_from=1,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command="true",
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        assert call_count[0] == 2

    def test_uncommitted_changes_exits(self, pipeline_env, monkeypatch):
        monkeypatch.setattr(qp.pipeline_mod, "git_has_uncommitted", lambda: True)
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )

    def test_hard_failure_stops_subsequent_rounds(
        self, tmp_path, pipeline_env, monkeypatch
    ):
        """In a multi-round scenario, hard failure should stop execution."""
        rounds_dir = tmp_path / "rounds"
        (rounds_dir / "02-refactor.md").write_text(
            "---\nname: refactor\ngate: hard\n---\nRefactor.\n"
        )
        (rounds_dir / "03-concurrency.md").write_text(
            "---\nname: concurrency\ngate: hard\n---\nConcurrency.\n"
        )
        executed = []
        def mock_run_round(rf, n, total, *args, **kwargs):
            rc = qp.parse_frontmatter(rf)
            executed.append(rc.name)
            if rc.name == "refactor":
                return qp.RoundOutcome.HARD_FAILED
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp.pipeline_mod, "run_round", mock_run_round)
        with pytest.raises(SystemExit):
            qp.pipeline(
                project_dir=None,
                rounds_arg=None,
                config_file=None,
                start_from=1,
                dry_run=False,
                worktree=False,
                worktree_symlinks=None,
                test_command="true",
                review_flag=None,
                log_dir_arg=str(pipeline_env / "logs"),
            )
        # Round 3 (concurrency) should never have been executed
        assert "concurrency" not in executed
        assert "refactor" in executed

    def test_config_file_loaded(self, tmp_path, pipeline_env, monkeypatch):
        cfg_file = tmp_path / "custom.yaml"
        cfg_file.write_text(yaml.dump({
            "test_command": "make test",
            "overrides": {"audit": {"gate": "soft"}},
        }))
        configs_seen = []
        def mock_run_round(rf, n, total, test_cmd, config, *args, **kwargs):
            configs_seen.append((test_cmd, config))
            return qp.RoundOutcome.PASSED
        monkeypatch.setattr(qp.pipeline_mod, "run_round", mock_run_round)
        qp.pipeline(
            project_dir=None,
            rounds_arg=None,
            config_file=str(cfg_file),
            start_from=1,
            dry_run=False,
            worktree=False,
            worktree_symlinks=None,
            test_command=None,
            review_flag=None,
            log_dir_arg=str(pipeline_env / "logs"),
        )
        assert configs_seen[0][0] == "make test"
        assert "audit" in configs_seen[0][1].overrides
