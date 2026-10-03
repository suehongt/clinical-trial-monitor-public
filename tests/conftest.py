"""pytest configuration and fixtures for clinical-trial-monitor tests."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def test_db():
    """Create a temporary database with full schema, then tear down.

    Overrides ``CONFIG.db.path``, closes any existing thread-local
    connection, creates the schema, and returns the path.  After the
    test the temp file is deleted and the original path restored.
    """
    from config import CONFIG as cfg
    from db.connection import close_connection
    from db.schema import create_schema

    original_path = cfg.db.path
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    cfg.db.path = tmp_path
    # Force reconnection to the new path
    close_connection()

    create_schema()

    yield tmp_path

    close_connection()
    os.unlink(str(tmp_path))
    cfg.db.path = original_path
