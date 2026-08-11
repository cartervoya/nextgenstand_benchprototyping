"""Shared test setup.

The one thing every test here needs is to be kept away from the real tuning
file. Without this the suite writes gains into the repo's tuning.json, tests
inherit each other's leftovers, and a `pytest` run quietly edits the operator's
bench configuration.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_tuning_file(tmp_path, monkeypatch):
    """Point the tuning store at a throwaway file for every test."""
    monkeypatch.setenv("NGS_TUNING_FILE", str(tmp_path / "tuning.json"))
