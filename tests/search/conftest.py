"""让 `from app.contracts... import ...` 在仓库里始终可用。

在仓库根目录跑 `python -m pytest` 时当前目录已在 `sys.path` 上，这里什么都不会做。
从别处调用 pytest 时，本文件向上找到含 `app/` 的那一层并加进去，所以
`tests/search/` 里的 import 语句与怎么调用无关。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

_repo_root = next(
    (p for p in [HERE, *HERE.parents] if (p / "app").is_dir()),
    None,
)

if _repo_root is not None and str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
