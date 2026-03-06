"""Shared fixtures and helpers for quality_pipeline tests."""

from __future__ import annotations

from unittest.mock import MagicMock


def _mock_git_fn(**defaults):
    """Factory for git() mocks — used by 11+ test classes."""
    def mock_git(*args, **kwargs):
        r = MagicMock()
        r.returncode = defaults.get("returncode", 0)
        r.stdout = defaults.get("stdout", "")
        r.stderr = defaults.get("stderr", "")
        return r
    return mock_git
