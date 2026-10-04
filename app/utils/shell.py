from __future__ import annotations

import logging
import os
from pathlib import Path

from app.utils.paths import data_dir, project_root

log = logging.getLogger(__name__)


def safe_open_path(path: str | Path, *, allow_roots: list[Path] | None = None) -> tuple[bool, str]:
    """
    Open a local path in Explorer/default app only if it resolves under allowed roots.
    Default roots: project dir + configured data/cache dir.
    """
    try:
        target = Path(path).expanduser().resolve()
    except OSError as e:
        return False, f"无效路径: {e}"

    if not target.exists():
        return False, "路径不存在"

    roots = allow_roots or [project_root(), data_dir()]
    # Also allow opening the extension install path when caller passes it explicitly
    # via allow_roots.

    ok = False
    for root in roots:
        try:
            target.relative_to(root.resolve())
            ok = True
            break
        except ValueError:
            continue
    if not ok:
        return False, "出于安全考虑，只能打开软件目录或缓存目录内的路径"

    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
        return True, ""
    except OSError as e:
        log.warning("startfile failed: %s", e)
        return False, f"无法打开: {e}"
