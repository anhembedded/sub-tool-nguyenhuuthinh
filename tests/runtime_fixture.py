"""Configured runtime for meaningful integration tests; no host-specific fallback."""
import os
from pathlib import Path
from hongguo_lite.common import config


def environment():
    path=Path(os.environ.get('WORKFLOW_TEST_CONFIG',Path(__file__).resolve().parents[1]/'config/local.json'))
    if not path.is_file():raise RuntimeError('Run setup, then set WORKFLOW_TEST_CONFIG to its generated config before testing')
    return config(path)['env']
