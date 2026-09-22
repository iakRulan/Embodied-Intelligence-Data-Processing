# -*- coding: utf-8 -*-
"""RefSync-QA 复赛算子入口：python run.py --input <数据集目录> --output <输出目录>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from refsync_qa.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
