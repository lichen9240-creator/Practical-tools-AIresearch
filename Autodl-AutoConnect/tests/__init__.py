"""共享测试基座：让 ``python -m unittest`` 能找到 ``autodl_autoconnect`` 包。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
