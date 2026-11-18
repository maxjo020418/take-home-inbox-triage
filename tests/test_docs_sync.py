"""ENGINEERING_LOG.md's schema appendix must match the code (regenerate with
`uv run python scripts/render_schema_notes.py`)."""

import importlib.util
from pathlib import Path


def test_schema_appendix_matches_code():
    spec = importlib.util.spec_from_file_location("rsn", Path("scripts/render_schema_notes.py"))
    rsn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rsn)
    assert rsn.render() in Path("ENGINEERING_LOG.md").read_text(), \
        "ENGINEERING_LOG.md appendix is stale: run `uv run python scripts/render_schema_notes.py`"
