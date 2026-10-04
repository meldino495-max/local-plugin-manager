from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.archive import ArchiveError, extract_archive
from app.core.updater import ExtensionUpdater, cleanup_work_dir
from app.models.extension import BrowserKind, ExtensionInfo
from app.models.store import AppStore
from app.utils.paths import ensure_runtime_dirs


def main() -> None:
    ensure_runtime_dirs()
    base = Path(tempfile.mkdtemp(prefix="bem_test_"))
    ext_dir = base / "ext"
    ext_dir.mkdir()
    (ext_dir / "manifest.json").write_text(
        json.dumps({"name": "Demo Ext", "version": "1.0.0", "manifest_version": 3}),
        encoding="utf-8",
    )
    (ext_dir / "a.txt").write_text("old-a", encoding="utf-8")
    (ext_dir / "keep.txt").write_text("keep-me", encoding="utf-8")

    pkg_root = base / "pkg" / "DemoExt"
    pkg_root.mkdir(parents=True)
    (pkg_root / "manifest.json").write_text(
        json.dumps({"name": "Demo Ext", "version": "2.0.0", "manifest_version": 3}),
        encoding="utf-8",
    )
    (pkg_root / "a.txt").write_text("new-a", encoding="utf-8")
    (pkg_root / "b.txt").write_text("new-b", encoding="utf-8")
    zip_path = base / "update.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for p in pkg_root.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(pkg_root.parent).as_posix())

    bad = base / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    try:
        extract_archive(bad, base / "bad_out")
        raise SystemExit("TRAVERSAL_FAIL")
    except ArchiveError:
        print("TRAVERSAL_OK")

    ext = ExtensionInfo(
        uid="test|Default|demo",
        browser=BrowserKind.CHROME,
        profile="Default",
        ext_id="demo",
        name="Demo Ext",
        version="1.0.0",
        description="",
        path=str(ext_dir),
    )
    store = AppStore()
    up = ExtensionUpdater(store)
    work, preview = up.prepare_upload(ext, zip_path)
    print("PREVIEW", preview.package_version, preview.file_count)
    res = up.apply_upload(ext, zip_path, work)
    cleanup_work_dir(work)
    assert res.ok, res.message
    assert (ext_dir / "a.txt").read_text(encoding="utf-8") == "new-a"
    assert (ext_dir / "b.txt").read_text(encoding="utf-8") == "new-b"
    assert (ext_dir / "keep.txt").read_text(encoding="utf-8") == "keep-me"
    assert json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))["version"] == "2.0.0"
    print("UPDATE_OK", res.new_version, res.written_count)

    snap = next(h for h in store.get_history(ext.uid) if h.source == "snapshot")
    res2 = up.restore_record(ext, snap)
    assert res2.ok, res2.message
    assert (ext_dir / "a.txt").read_text(encoding="utf-8") == "old-a"
    print("ROLLBACK_OK")

    res3 = up.restore_latest(ext)
    assert res3.ok, res3.message
    assert (ext_dir / "a.txt").read_text(encoding="utf-8") == "new-a"
    print("LATEST_OK", len(store.get_history(ext.uid)))
    shutil.rmtree(base, ignore_errors=True)
    print("DONE")


if __name__ == "__main__":
    main()
