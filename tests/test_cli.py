"""Tests for quality_pipeline.__main__ — Click CLI argument parsing."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from quality_pipeline.__main__ import cli


class TestCliBasicArgs:
    """Test that CLI arguments are parsed and forwarded to pipeline()."""

    def test_default_args(self):
        """Bare invocation passes all defaults to pipeline()."""
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            result = runner.invoke(cli, [])
        assert result.exit_code == 0 or mock_pl.called
        if mock_pl.called:
            kw = mock_pl.call_args
            assert kw[1]["project_dir"] is None
            assert kw[1]["rounds_arg"] is None
            assert kw[1]["dry_run"] is False
            assert kw[1]["worktree"] is False
            assert kw[1]["start_from"] == 1
            assert kw[1]["meta_review"] is False

    def test_dry_run_flag(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--dry-run"])
        mock_pl.assert_called_once()
        assert mock_pl.call_args[1]["dry_run"] is True

    def test_worktree_flag(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--worktree"])
        assert mock_pl.call_args[1]["worktree"] is True

    def test_rounds_arg(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--rounds", "audit refactor"])
        assert mock_pl.call_args[1]["rounds_arg"] == "audit refactor"

    def test_project_dir(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--project-dir", "/tmp/myproject"])
        assert mock_pl.call_args[1]["project_dir"] == "/tmp/myproject"

    def test_config_file(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--config", "/tmp/custom.yaml"])
        assert mock_pl.call_args[1]["config_file"] == "/tmp/custom.yaml"

    def test_start_from(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--start-from", "3"])
        assert mock_pl.call_args[1]["start_from"] == 3

    def test_test_command(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--test-command", "pytest -x"])
        assert mock_pl.call_args[1]["test_command"] == "pytest -x"

    def test_review_flag_on(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--review"])
        assert mock_pl.call_args[1]["review_flag"] is True

    def test_review_flag_off(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--no-review"])
        assert mock_pl.call_args[1]["review_flag"] is False

    def test_review_flag_default(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, [])
        assert mock_pl.call_args[1]["review_flag"] is None

    def test_meta_review_flag(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--meta-review"])
        assert mock_pl.call_args[1]["meta_review"] is True

    def test_log_dir(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--log-dir", "/tmp/logs"])
        assert mock_pl.call_args[1]["log_dir_arg"] == "/tmp/logs"

    def test_worktree_symlinks(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, ["--worktree-symlinks", "node_modules .venv"])
        assert mock_pl.call_args[1]["worktree_symlinks"] == "node_modules .venv"


class TestCliValidation:
    """Test CLI validation and error handling."""

    def test_start_from_zero_rejected(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline"):
            result = runner.invoke(cli, ["--start-from", "0"])
        assert result.exit_code != 0
        assert "positive integer" in result.output.lower() or "must be" in result.output.lower()

    def test_start_from_negative_rejected(self):
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline"):
            result = runner.invoke(cli, ["--start-from", "-1"])
        assert result.exit_code != 0

    def test_start_from_non_integer_rejected(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--start-from", "abc"])
        assert result.exit_code != 0

    def test_pipeline_exception_propagates(self):
        """Exceptions from pipeline() should surface as non-zero exit."""
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline", side_effect=SystemExit(1)):
            result = runner.invoke(cli, [])
        assert result.exit_code == 1

    def test_multiple_flags_combined(self):
        """Multiple flags should all be forwarded correctly."""
        runner = CliRunner()
        with patch("quality_pipeline.__main__.pipeline") as mock_pl:
            runner.invoke(cli, [
                "--dry-run", "--worktree", "--meta-review",
                "--start-from", "2", "--rounds", "audit",
            ])
        kw = mock_pl.call_args[1]
        assert kw["dry_run"] is True
        assert kw["worktree"] is True
        assert kw["meta_review"] is True
        assert kw["start_from"] == 2
        assert kw["rounds_arg"] == "audit"
