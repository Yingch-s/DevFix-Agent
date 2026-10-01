"""benchmark 工具：强制删除目录（Windows 下 .git/objects 为只读）。"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

_RMTREE_ATTEMPTS = 3
_RETRY_DELAY_S = 2.0


def force_rmtree(path: Path) -> None:
    """先清只读属性再删除；瞬态文件锁（如刚退出的 JVM/杀毒扫描）重试。"""
    if not path.exists():
        return
    for p in path.rglob("*"):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    for attempt in range(_RMTREE_ATTEMPTS):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(_RETRY_DELAY_S)
    raise RuntimeError(f"无法删除 {path}：请手动删除后重试（可能有进程占用）")
