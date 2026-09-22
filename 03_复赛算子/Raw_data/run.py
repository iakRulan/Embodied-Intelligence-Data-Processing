# -*- coding: utf-8 -*-
"""RefSync-QA entry: python run.py --input /data/test --output /data/result"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from refsync_qa.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
