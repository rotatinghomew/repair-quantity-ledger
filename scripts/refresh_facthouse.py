#!/usr/bin/env python3
"""事实仓刷新/追查入口（隔离模式运行）。

示例：
    python3 -I scripts/refresh_facthouse.py --facts data/facts.json --out build/facthouse.json
    python3 -I scripts/refresh_facthouse.py --facts data/facts.json --ledger build/facthouse.json
    python3 -I scripts/refresh_facthouse.py --facts data/facts.json --trace CERT-81
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facthouse.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
