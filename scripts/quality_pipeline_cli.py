#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["click>=8.0", "pyyaml>=6.0"]
# ///
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_pipeline.__main__ import cli
cli()
