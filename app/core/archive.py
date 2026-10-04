from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# Limits against zip/7z bombs when extracting untrusted packages.
MAX_EXTRACT_BYTES = 512 * 1024 * 1024  # 512 MiB uncompressed total
MAX_EXTRACT_FILES = 50_000
MAX_COMPRESSION_RATIO = 200  # uncompressed/compressed soft check for zip members

_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ArchiveError(Exception):
    pass


def is_within_dir(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


# Back-compat alias used by updater
_is_within = is_within_dir


def _safe_join(base: Path, member: str) -> Path:
    """
    Join archive member into base while blocking zip-slip variants, including
    Windows absolute paths (Path(base) / 'C:/Windows' can escape on Win32).
    """
    if member is None:
        raise ArchiveError("拒绝空路径成员")
    if "\x00" in member:
        raise ArchiveError(f"拒绝含空字节的路径: {member!r}")

    cleaned = member.replace("\\", "/")
    # UNC / absolute POSIX
    if cleaned.startswith("/") or cleaned.startswith("//"):
        raise ArchiveError(f"拒绝绝对路径: {member}")
    # Windows drive / device paths
    if _ABS_DRIVE_RE.match(cleaned) or cleaned.startswith("\\\\"):
        raise ArchiveError(f"拒绝盘符/UNC 路径: {member}")
    if cleaned.startswith("./") and _ABS_DRIVE_RE.match(cleaned[2:]):
        raise ArchiveError(f"拒绝伪装绝对路径: {member}")

    parts: list[str] = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ArchiveError(f"拒绝路径穿越: {member}")
        # Reject Windows device names used as a path segment
        if part.upper().split(".")[0] in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM2",
            "LPT1",
            "LPT2",
        }:
            raise ArchiveError(f"拒绝保留设备名: {member}")
        parts.append(part)

    if not parts:
        # Directory entry for root — map to base
        return base.resolve()

    # Build without using Path / operator on absolute-looking right-hand sides
    dest = base.resolve()
    for part in parts:
        dest = dest.joinpath(part)
    dest = dest.resolve()
    if not _is_within(base, dest):
        raise ArchiveError(f"拒绝越界路径: {member}")
    return dest


def _assert_tree_safe(root: Path) -> None:
    """After extraction, ensure no symlink escape and all files stay in root."""
    root = root.resolve()
    count = 0
    total = 0
    for path in root.rglob("*"):
        count += 1
        if count > MAX_EXTRACT_FILES:
            raise ArchiveError(f"解压文件数超过限制（{MAX_EXTRACT_FILES}）")
        if path.is_symlink():
            # Do not allow symlinks from untrusted archives
            try:
                path.unlink()
            except OSError:
                pass
            raise ArchiveError(f"拒绝符号链接: {path.relative_to(root)}")
        try:
            resolved = path.resolve()
        except OSError as e:
            raise ArchiveError(f"无法解析解压路径: {e}") from e
        if not _is_within(root, resolved):
            raise ArchiveError(f"解压结果越界: {path}")
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total > MAX_EXTRACT_BYTES:
                raise ArchiveError(
                    f"解压体积超过限制（{MAX_EXTRACT_BYTES // (1024 * 1024)} MB）"
                )


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract .7z / .7zip / .zip into dest with path-traversal checks."""
    archive = archive.resolve()
    dest = dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    suffix = archive.suffix.lower()
    name_lower = archive.name.lower()
    if suffix in {".7z", ".7zip"} or name_lower.endswith(".7zip"):
        _extract_7z(archive, dest)
    elif suffix == ".zip":
        _extract_zip(archive, dest)
    else:
        # Try 7z first, then zip
        try:
            _extract_7z(archive, dest)
        except ArchiveError:
            raise
        except Exception:
            _extract_zip(archive, dest)

    _assert_tree_safe(dest)


def _extract_zip(archive: Path, dest: Path) -> None:
    try:
        compressed_size = archive.stat().st_size
        with zipfile.ZipFile(archive, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_EXTRACT_FILES:
                raise ArchiveError(f"压缩包条目过多（>{MAX_EXTRACT_FILES}）")

            total_uncomp = 0
            for info in infos:
                # Skip directory markers after path check
                name = info.filename
                target = _safe_join(dest, name)
                if name.endswith("/") or info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                total_uncomp += int(info.file_size or 0)
                if total_uncomp > MAX_EXTRACT_BYTES:
                    raise ArchiveError(
                        f"压缩包解压体积超过限制（{MAX_EXTRACT_BYTES // (1024 * 1024)} MB）"
                    )
                if (
                    info.compress_size
                    and info.file_size
                    and info.compress_size > 0
                    and (info.file_size / info.compress_size) > MAX_COMPRESSION_RATIO
                    and info.file_size > 10 * 1024 * 1024
                ):
                    # Soft bomb signal for a single huge sparse member
                    if compressed_size < 1024 * 1024 and total_uncomp > 100 * 1024 * 1024:
                        raise ArchiveError("疑似解压炸弹，已拒绝")

                target.parent.mkdir(parents=True, exist_ok=True)
                # Refuse writing through existing symlink parents
                if any(p.is_symlink() for p in target.parents if _is_within(dest, p)):
                    raise ArchiveError(f"拒绝经符号链接写入: {name}")

                with zf.open(info, "r") as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 64)
    except ArchiveError:
        raise
    except Exception as e:
        raise ArchiveError(f"ZIP 解压失败: {e}") from e


def _extract_7z(archive: Path, dest: Path) -> None:
    try:
        import py7zr
    except ImportError as e:
        raise ArchiveError("未安装 py7zr，无法解压 7z。请执行: pip install py7zr") from e

    try:
        # max_extract_size requires py7zr >= 1.1.3
        with py7zr.SevenZipFile(
            archive, mode="r", max_extract_size=MAX_EXTRACT_BYTES
        ) as zf:
            names = zf.getnames()
            if len(names) > MAX_EXTRACT_FILES:
                raise ArchiveError(f"压缩包条目过多（>{MAX_EXTRACT_FILES}）")
            for name in names:
                _safe_join(dest, name)
            zf.extractall(path=dest)
            for name in names:
                _safe_join(dest, name)
    except ArchiveError:
        raise
    except Exception as e:
        raise ArchiveError(f"7z 解压失败: {e}") from e


def copy_tree_merge(src: Path, dst: Path) -> tuple[list[str], list[str]]:
    """
    Copy all files from src into dst, overwriting existing files.
    Does not delete files that exist only in dst.
    Returns (created_or_overwritten, skipped).
    """
    src = src.resolve()
    dst = dst.resolve()
    if not src.is_dir():
        raise ArchiveError("源目录不存在")
    if not dst.is_dir():
        raise ArchiveError("目标插件目录不存在")

    written: list[str] = []
    skipped: list[str] = []

    for path in src.rglob("*"):
        if path.is_symlink():
            skipped.append(str(path.relative_to(src)))
            continue
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        # Skip junk
        if "__MACOSX" in rel.parts or rel.name.startswith("._"):
            skipped.append(str(rel))
            continue
        # Rebuild target without Path / absolute RHS issues
        target = dst
        for part in rel.parts:
            if part in ("..",):
                raise ArchiveError(f"拒绝写入越界文件: {rel}")
            target = target.joinpath(part)
        target = target.resolve()
        if not _is_within(dst, target):
            raise ArchiveError(f"拒绝写入越界文件: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written.append(str(rel).replace("\\", "/"))

    return written, skipped


def snapshot_directory(src: Path, dest: Path) -> None:
    """Full directory snapshot for rollback."""
    src = src.resolve()
    dest = dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False)


def _iter_pack_files(src: Path) -> list[Path]:
    files: list[Path] = []
    for path in src.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(src)
        if "__MACOSX" in rel.parts or rel.name.startswith("._"):
            continue
        files.append(path)
    return files


def create_zip(src_dir: Path, dest_archive: Path) -> None:
    """Pack directory contents into a zip (paths relative to src_dir)."""
    src_dir = src_dir.resolve()
    if not src_dir.is_dir():
        raise ArchiveError("导出源目录不存在")
    dest_archive = dest_archive.resolve()
    dest_archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_archive.with_suffix(dest_archive.suffix + ".partial")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in _iter_pack_files(src_dir):
                arcname = path.relative_to(src_dir).as_posix()
                zf.write(path, arcname)
        if dest_archive.exists():
            dest_archive.unlink()
        tmp.replace(dest_archive)
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise ArchiveError(f"导出 ZIP 失败: {e}") from e


def create_7z(src_dir: Path, dest_archive: Path) -> None:
    """Pack directory contents into a 7z archive."""
    try:
        import py7zr
    except ImportError as e:
        raise ArchiveError("未安装 py7zr，无法导出 7z。请执行: pip install py7zr") from e

    src_dir = src_dir.resolve()
    if not src_dir.is_dir():
        raise ArchiveError("导出源目录不存在")
    dest_archive = dest_archive.resolve()
    dest_archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_archive.with_suffix(dest_archive.suffix + ".partial")
    try:
        with py7zr.SevenZipFile(tmp, mode="w") as zf:
            for path in _iter_pack_files(src_dir):
                arcname = path.relative_to(src_dir).as_posix()
                zf.write(path, arcname)
        if dest_archive.exists():
            dest_archive.unlink()
        tmp.replace(dest_archive)
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise ArchiveError(f"导出 7z 失败: {e}") from e


def create_archive(src_dir: Path, dest_archive: Path, fmt: str) -> None:
    """fmt: 'zip' | '7z'"""
    fmt = fmt.lower().lstrip(".")
    if fmt == "zip":
        create_zip(src_dir, dest_archive)
    elif fmt in {"7z", "7zip"}:
        if not dest_archive.suffix.lower().endswith("7z"):
            dest_archive = dest_archive.with_suffix(".7z")
        create_7z(src_dir, dest_archive)
    else:
        raise ArchiveError(f"不支持的导出格式: {fmt}")
