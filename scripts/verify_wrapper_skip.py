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

from app.core.archive import extract_archive
from app.core.manifest import describe_extension_root, find_extension_root, parse_manifest
from app.core.updater import ExtensionUpdater, cleanup_work_dir
from app.models.extension import BrowserKind, ExtensionInfo
from app.models.store import AppStore
from app.utils.paths import ensure_runtime_dirs


def make_ext_dir(path: Path, version: str, marker: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(
        json.dumps(
            {"name": "WrapTest", "version": version, "manifest_version": 3},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (path / "background.js").write_text(f"// {marker}\n", encoding="utf-8")


def main() -> None:
    ensure_runtime_dirs()
    base = Path(tempfile.mkdtemp(prefix="bem_wrap_verify_"))
    print("BASE", base)

    installed = base / "installed"
    make_ext_dir(installed, "1.0.0", "OLD")
    (installed / "keep_old.txt").write_text("OLD_KEEP", encoding="utf-8")

    cases: list[tuple[str, Path, str, str, str]] = []

    # A: single wrapper folder
    a = base / "caseA_src"
    make_ext_dir(a / "MyPlugin-v2", "2.0.0", "CASE_A")
    (a / "MyPlugin-v2" / "new_a.txt").write_text("A", encoding="utf-8")
    za = base / "caseA.zip"
    with zipfile.ZipFile(za, "w") as zf:
        for p in (a / "MyPlugin-v2").rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(a).as_posix())
    cases.append(("A_single_wrapper", za, "2.0.0", "CASE_A", "MyPlugin-v2"))

    # B: double wrapper + README sibling
    b = base / "caseB_src"
    make_ext_dir(b / "release" / "ext", "3.1.0", "CASE_B")
    (b / "README.md").write_text("# hi", encoding="utf-8")
    (b / "release" / "ext" / "new_b.txt").write_text("B", encoding="utf-8")
    zb = base / "caseB.zip"
    with zipfile.ZipFile(zb, "w") as zf:
        for p in b.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(b).as_posix())
    cases.append(("B_double_plus_readme", zb, "3.1.0", "CASE_B", "release/ext"))

    # C: 7z wrapper
    try:
        import py7zr

        c = base / "caseC_src"
        make_ext_dir(c / "PackedExt", "4.0.0", "CASE_C")
        (c / "PackedExt" / "new_c.txt").write_text("C", encoding="utf-8")
        zc = base / "caseC.7z"
        with py7zr.SevenZipFile(zc, "w") as zf:
            for p in (c / "PackedExt").rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(c).as_posix())
        cases.append(("C_7z_wrapper", zc, "4.0.0", "CASE_C", "PackedExt"))
    except Exception as e:  # noqa: BLE001
        print("SKIP_7Z", e)

    ext = ExtensionInfo(
        uid="verify|Default|wraptest",
        browser=BrowserKind.CHROME,
        profile="Default",
        ext_id="wraptest",
        name="WrapTest",
        version="1.0.0",
        description="",
        path=str(installed),
    )
    store = AppStore()
    up = ExtensionUpdater(store)

    for name, archive, expect_ver, marker, expect_inner in cases:
        print("\n====", name, "====")
        if archive.suffix.lower() == ".7z":
            import py7zr

            with py7zr.SevenZipFile(archive, "r") as zf:
                print("members:", zf.getnames()[:20])
        else:
            with zipfile.ZipFile(archive) as zf:
                print("members:", zf.namelist()[:20])

        tmp = base / f"extract_{name}"
        if tmp.exists():
            shutil.rmtree(tmp)
        extract_archive(archive, tmp)
        root = find_extension_root(tmp)
        inner = describe_extension_root(tmp, root) if root else None
        print("find_root:", root)
        print("inner_rel:", inner)
        assert root is not None, "root missing"
        assert (root / "manifest.json").is_file()
        assert inner is not None
        assert inner.replace("\\", "/") == expect_inner, (inner, expect_inner)
        info = parse_manifest(root)
        assert info and info["version"] == expect_ver, info

        work, preview = up.prepare_upload(ext, archive)
        print("preview.inner:", preview.archive_inner_root)
        print("preview.files:", preview.sample_files[:8])
        print("preview.version:", preview.package_version)
        assert preview.package_version == expect_ver
        assert preview.archive_inner_root.replace("\\", "/") == expect_inner
        for f in preview.sample_files:
            assert not f.startswith("MyPlugin-v2/"), f
            assert not f.startswith("release/"), f
            assert not f.startswith("PackedExt/"), f

        res = up.apply_upload(ext, archive, work, note=name)
        cleanup_work_dir(work)
        print("apply_ok:", res.ok, "written:", res.written_count, "ver:", res.new_version)
        assert res.ok, res.message.encode("unicode_escape").decode("ascii")

        assert (installed / "manifest.json").is_file()
        man = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
        assert man["version"] == expect_ver, man
        bg = (installed / "background.js").read_text(encoding="utf-8")
        assert marker in bg, bg
        assert not (installed / "MyPlugin-v2").exists(), "wrapper leaked"
        assert not (installed / "release").exists(), "wrapper leaked"
        assert not (installed / "PackedExt").exists(), "wrapper leaked"
        assert not (installed / "README.md").exists(), "outer readme leaked"
        assert (installed / "keep_old.txt").read_text(encoding="utf-8") == "OLD_KEEP"
        print("OK", name)

    print("\nALL_CASES_PASSED")
    print("final install listing:", sorted(p.name for p in installed.iterdir()))
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
