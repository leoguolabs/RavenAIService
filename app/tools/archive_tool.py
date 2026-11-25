"""
Archive extraction tool with safe decompression and nested archive handling.
Outputs tree/XML to support AI log analysis decisions.
"""
import os
import tarfile
import zipfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from app.config import settings
from app.agents.xml_utils import wrap_file_list, wrap_metadata, wrap_document
from app.tools.fs_tools import safe_listdir

SUPPORTED_ARCHIVE_EXTS = {".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar", ".zip", ".7z", ".rar"}

# Plain-text log formats that can be analyzed directly without decompression.
SUPPORTED_TEXT_EXTS = {
    ".log", ".txt", ".out", ".err", ".trace", ".json", ".xml", ".csv", ".tsv",
    ".md", ".markdown", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml", ".properties",
}

# Binary spreadsheet files that must be copied into the workspace verbatim.
# .xlsx/.xlsm are ZIP containers internally, so they must be detected before
# archive probing or they will be decompressed into Office XML internals.
SUPPORTED_SPREADSHEET_EXTS = {".xls", ".xlsx", ".xlsm"}
SUPPORTED_PACKET_CAPTURE_EXTS = (".pcap", ".pcap0")

# (magic_bytes, byte_offset) for each supported extension
ArchiveMagic = Union[Tuple[bytes, int], List[Tuple[bytes, int]]]
ARCHIVE_MAGIC: Dict[str, ArchiveMagic] = {
    ".tar.gz":  (b"\x1f\x8b", 0),
    ".tgz":     (b"\x1f\x8b", 0),
    ".tar.bz2": (b"BZh", 0),
    ".tar.xz":  (b"\xfd7zXZ\x00", 0),
    ".zip":     (b"PK\x03\x04", 0),
    ".7z":      (b"7z\xbc\xaf\x27\x1c", 0),
    ".rar": [
        (b"Rar!\x1a\x07\x00", 0),      # RAR 1.5-4.x
        (b"Rar!\x1a\x07\x01\x00", 0),  # RAR 5.x
    ],
    ".tar":     (b"ustar", 257),
}


def _is_in_allowed_root(path: str) -> bool:
    try:
        abs_path = os.path.abspath(path)
        abs_root = os.path.abspath(settings.agent_root_dir)
        return os.path.commonpath([abs_path, abs_root]) == abs_root
    except Exception:
        return False


def check_archive_magic(header: bytes, ext: str) -> bool:
    """Return True if header bytes match the expected magic for the given extension."""
    info = ARCHIVE_MAGIC.get(ext.lower())
    if info is None:
        return False
    candidates = info if isinstance(info, list) else [info]
    for magic, offset in candidates:
        end = offset + len(magic)
        if len(header) >= end and header[offset:end] == magic:
            return True
    return False


def guess_archive_type(path: str) -> Optional[str]:
    """Detect archive type from extension, falling back to content inspection."""
    suffix = "".join(Path(path).suffixes).lower()
    if suffix in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar"):
        return "tar"
    if suffix == ".zip":
        return "zip"
    if suffix == ".7z":
        return "7z"
    if suffix == ".rar":
        return "rar"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
        if zipfile.is_zipfile(path):
            return "zip"
        try:
            import rarfile
            if rarfile.is_rarfile(path):
                return "rar"
        except ImportError:
            pass
    except Exception:
        pass
    return None


def looks_like_text(path: str, *, sample_size: int = 8192) -> bool:
    """Heuristic, content-based check for whether ``path`` is a plain-text file.

    A leading sample is treated as text when it is empty, carries no NUL byte,
    does not start with a known archive/compression magic, and decodes as UTF-8
    (tolerating a multi-byte sequence truncated at the sample boundary). This is
    deliberately conservative: binary blobs and compressed archives are rejected
    so they are never copied in verbatim as if they were logs.
    """
    try:
        with open(path, "rb") as f:
            sample = f.read(sample_size)
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    # Reject anything whose header matches a known archive/compression magic,
    # even when it carries a text-ish extension.
    for ext in ARCHIVE_MAGIC:
        if check_archive_magic(sample, ext):
            return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # A multi-byte UTF-8 sequence may straddle the sample boundary; retry
        # after dropping the last few bytes before declaring it non-text.
        try:
            sample[:-3].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False


def detect_upload_kind(path: str) -> str:
    """Pre-classify an uploaded file as ``"archive"``, ``"text"``,
    ``"spreadsheet"``, ``"pcap"`` or ``"unknown"``.

    Archive detection (extension + content inspection) takes precedence so a
    text-named archive is still decompressed. Spreadsheet extensions are checked
    before archive probing because .xlsx/.xlsm files are ZIP containers and must
    remain intact for Excel tooling. Packet captures also go directly to their
    reader. ``"unknown"`` covers binary blobs we can neither extract nor analyze.
    """
    suffix = Path(path).suffix.lower()
    if suffix in SUPPORTED_SPREADSHEET_EXTS:
        return "spreadsheet"
    if suffix in SUPPORTED_PACKET_CAPTURE_EXTS:
        return "pcap"
    if guess_archive_type(path) is not None:
        return "archive"
    if looks_like_text(path):
        return "text"
    return "unknown"


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _safe_join(base: str, *paths: str) -> str:
    candidate = os.path.abspath(os.path.join(base, *paths))
    base_abs = os.path.abspath(base)
    if os.path.commonpath([candidate, base_abs]) != base_abs:
        raise PermissionError(f"Unsafe path detected: {candidate}")
    return candidate


def _safe_extract_tar(src: str, dest: str) -> None:
    with tarfile.open(src, mode="r:*") as tf:
        for m in tf.getmembers():
            # skip dirs, symlinks, device files
            if m.isdev() or m.issym() or m.ischr() or m.isfifo():
                continue
            target = _safe_join(dest, m.name)
            if m.isdir():
                _ensure_dir(target)
                continue
            parent = os.path.dirname(target)
            _ensure_dir(parent)
            f = tf.extractfile(m)
            if f is None:
                continue
            with open(target, "wb") as out:
                out.write(f.read())


def _safe_extract_zip(src: str, dest: str) -> None:
    with zipfile.ZipFile(src, mode="r") as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                # directory
                _ensure_dir(_safe_join(dest, name))
                continue
            target = _safe_join(dest, name)
            parent = os.path.dirname(target)
            _ensure_dir(parent)
            with zf.open(name) as f, open(target, "wb") as out:
                out.write(f.read())


def _safe_extract_7z(src: str, dest: str) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError("py7zr is required to extract .7z archives") from exc
    with py7zr.SevenZipFile(src, mode="r") as sz:
        for name, bio in sz.read().items():
            if bio is None:
                continue
            target = _safe_join(dest, name)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(bio.read())


def _safe_extract_rar(src: str, dest: str) -> None:
    try:
        import rarfile
    except ImportError as exc:
        raise RuntimeError("rarfile is required to extract .rar archives") from exc

    with rarfile.RarFile(src, mode="r") as rf:
        for info in rf.infolist():
            name = info.filename
            if not name or name.endswith("/") or info.isdir():
                _ensure_dir(_safe_join(dest, name))
                continue
            is_symlink = getattr(info, "is_symlink", None)
            if callable(is_symlink) and is_symlink():
                continue
            target = _safe_join(dest, name)
            parent = os.path.dirname(target)
            _ensure_dir(parent)
            with rf.open(info) as f, open(target, "wb") as out:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)


def compute_extract_root(archive_path: str) -> str:
    base = os.path.basename(archive_path)
    suffixes = "".join(Path(base).suffixes)
    name = base[: -len(suffixes)] if suffixes else base.rsplit(".", 1)[0]
    unique = uuid.uuid4().hex[:8]
    extract_base = os.path.join(settings.agent_root_dir, "_extracted")
    _ensure_dir(extract_base)
    return os.path.join(extract_base, f"{name}-{unique}")


def safe_extract_archive(archive_path: str, dest_root: Optional[str] = None) -> str:
    if not settings.agent_enabled:
        raise RuntimeError("Agent disabled by configuration")
    if not _is_in_allowed_root(archive_path):
        raise PermissionError(f"Archive outside allowed root: {archive_path}")
    if not os.path.exists(archive_path):
        raise FileNotFoundError(archive_path)

    a_type = guess_archive_type(archive_path)
    if a_type is None:
        raise ValueError("Unsupported archive format")

    dest = dest_root or compute_extract_root(archive_path)
    _ensure_dir(dest)

    if a_type == "tar":
        _safe_extract_tar(archive_path, dest)
    elif a_type == "zip":
        _safe_extract_zip(archive_path, dest)
    elif a_type == "7z":
        _safe_extract_7z(archive_path, dest)
    else:
        _safe_extract_rar(archive_path, dest)

    return dest


def list_tree_xml(root_dir: str, max_depth: int = 2) -> str:
    files = safe_listdir(root=root_dir, include_glob=None, max_depth=max_depth)
    return wrap_file_list(files, source=root_dir)


def find_nested_archives(root_dir: str) -> List[Dict[str, str]]:
    nested: List[Dict[str, str]] = []
    for d, _, files in os.walk(root_dir):
        for f in files:
            path = os.path.join(d, f)
            if any(f.endswith(ext) for ext in SUPPORTED_ARCHIVE_EXTS):
                try:
                    st = os.stat(path)
                    nested.append({
                        "path": path,
                        "size": str(st.st_size),
                        "modified": str(int(st.st_mtime))
                    })
                except Exception:
                    continue
    return nested


def nested_archives_xml(root_dir: str) -> str:
    files = find_nested_archives(root_dir)
    return wrap_document(wrap_file_list(files, source="nested_archives"), {"type": "nested_archives"})


def extract_nested_archive_xml(nested_path: str, parent_root: Optional[str] = None) -> Tuple[str, str]:
    if not _is_in_allowed_root(nested_path):
        raise PermissionError(f"Nested archive outside allowed root: {nested_path}")
    dest = safe_extract_archive(nested_path, dest_root=None)
    xml = wrap_document(list_tree_xml(dest, max_depth=2), {"extracted": dest, "source": os.path.basename(nested_path)})
    return dest, xml


def auto_extract_archive_xml(archive_path: str) -> Tuple[str, str]:
    """Extract top-level archive and return (extracted_dir, xml)."""
    dest = safe_extract_archive(archive_path)
    tree_xml = list_tree_xml(dest, max_depth=2)
    meta_xml = wrap_metadata({"archive_path": archive_path, "extracted_dir": dest})
    return dest, wrap_document(meta_xml + tree_xml, {"type": "extraction"})
