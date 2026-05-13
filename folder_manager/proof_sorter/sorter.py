from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from folder_manager.proofing_online.passwords import format_proofing_password_folder_name

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"}
SORTED_SUBDIR_NAME = "sorted"
FORBIDDEN_FS_CHARS = set('\\\n/:*?"<>|')
AUTO_PASSWORD_SALT = "ProofSorterAutoPwV1"
TRAILING_PARENS_STRIPPER = re.compile(r"\)+\s*$")
TRAILING_PARENS_RE = re.compile(r"\((?P<inner>[^()]*)\)\s*$")


@dataclass
class StudentBucket:
    name: str
    password: str
    files: List[Path] = field(default_factory=list)


@dataclass
class FolderSortResult:
    source_folder: str
    sorted_folder: Optional[str]
    student_count: int
    file_count: int
    moved_count: int
    dry_run: bool
    skipped_reason: Optional[str] = None


LogFn = Optional[Callable[[str], None]]
ProgressFn = Optional[Callable[[int, int, str], None]]


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def _sanitize_filename_part(text: str) -> str:
    out = "".join(" " if c in FORBIDDEN_FS_CHARS else c for c in text)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _ensure_unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    parent = dest.parent
    idx = 2
    while True:
        candidate = parent / f"{stem} ({idx}){suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def _generate_stable_code(label: str) -> str:
    digest = hashlib.sha256(f"{AUTO_PASSWORD_SALT}|{label}".encode("utf-8")).hexdigest()
    num = int(digest[:8], 16) % 10000
    if num == 0:
        num = 1
    return f"{num:04d}"


def _extract_bucket_from_parentheses(
    stem: str,
    generic_password: str,
    auto_password: bool,
) -> Tuple[Optional[str], str]:
    trimmed = TRAILING_PARENS_STRIPPER.sub(")", stem)
    match = TRAILING_PARENS_RE.search(trimmed)
    if not match:
        return None, generic_password

    inner = _sanitize_filename_part(match.group("inner").strip())
    if not inner:
        return None, generic_password

    if "_" in inner:
        had_trailing = inner.endswith("_")
        base, pw_candidate = inner.rsplit("_", 1)
        base = base.strip()
        pw_candidate = pw_candidate.strip()

        if not pw_candidate and "_" in base:
            base, pw_candidate = base.rsplit("_", 1)
            base = base.strip()
            pw_candidate = pw_candidate.strip()

        use_auto = auto_password and (not pw_candidate)
        if use_auto:
            pw = _generate_stable_code(base or inner)
            folder_name = format_proofing_password_folder_name(base or inner, pw)
        else:
            pw = pw_candidate or generic_password
            if pw_candidate:
                if re.fullmatch(r"\d{4}", pw_candidate):
                    folder_name = format_proofing_password_folder_name(base, pw)
                else:
                    folder_name = f"{base}_{pw}_" if had_trailing else inner
            else:
                folder_name = format_proofing_password_folder_name(base, pw)
        return folder_name, pw

    pw = _generate_stable_code(inner) if auto_password else generic_password
    folder_name = format_proofing_password_folder_name(inner, pw)
    return folder_name, pw


def _collect_images(root: Path) -> List[Path]:
    images: List[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        _dirnames[:] = [name for name in _dirnames if name.lower() != SORTED_SUBDIR_NAME]
        base = Path(dirpath)
        for filename in filenames:
            candidate = base / filename
            if _is_image(candidate):
                images.append(candidate)
    return sorted(images, key=lambda p: str(p.relative_to(root)).lower())


def _build_buckets(root: Path, generic_password: str, auto_password: bool) -> Dict[str, StudentBucket]:
    buckets: Dict[str, StudentBucket] = {}
    current_bucket_name: Optional[str] = None

    for img in _collect_images(root):
        bucket_name, pw = _extract_bucket_from_parentheses(img.stem, generic_password, auto_password)

        if bucket_name:
            current_bucket_name = bucket_name
            bucket = buckets.get(current_bucket_name)
            if bucket is None:
                bucket = StudentBucket(name=current_bucket_name, password=pw)
                buckets[current_bucket_name] = bucket
            else:
                if bucket.password == generic_password and pw != generic_password:
                    bucket.password = pw
        else:
            if current_bucket_name is None:
                if auto_password:
                    pw = _generate_stable_code("Unassigned")
                    current_bucket_name = f"Unassigned_{pw}"
                else:
                    pw = generic_password
                    current_bucket_name = f"Unassigned_{generic_password}"
                if current_bucket_name not in buckets:
                    buckets[current_bucket_name] = StudentBucket(name=current_bucket_name, password=pw)

        buckets[current_bucket_name].files.append(img)

    return buckets


def _create_cover_jpg(dst_folder: Path, student: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return

    width, height = 1000, 700
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    text_width = draw.textlength(student)
    draw.text(((width - text_width) / 2, (height - 40) / 2), student, fill="black")
    out = dst_folder / "#COVER.jpg"
    dst_folder.mkdir(parents=True, exist_ok=True)
    image.save(out, "JPEG", quality=90, optimize=True)


def _move_or_copy(src: Path, dst: Path, do_copy: bool) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = _ensure_unique_path(dst)
    if do_copy:
        shutil.copy2(src, dst)
    else:
        shutil.move(src, dst)
    return dst


def _apply_sort(
    root: Path,
    buckets: Dict[str, StudentBucket],
    *,
    output_folder: Optional[Path] = None,
    make_covers: bool,
    copy_files: bool,
    dry_run: bool,
    log: LogFn,
) -> Tuple[Path, int]:
    sorted_root = output_folder or (root / SORTED_SUBDIR_NAME)
    if not dry_run:
        sorted_root.mkdir(parents=True, exist_ok=True)

    moved_count = 0
    for student in sorted(buckets.keys(), key=lambda s: s.lower()):
        bucket = buckets[student]
        safe_name = _sanitize_filename_part(student)
        dst_folder = sorted_root / safe_name

        if make_covers and not dry_run:
            _create_cover_jpg(dst_folder, student)

        for idx, src in enumerate(bucket.files):
            dst_path = dst_folder / f"{src.stem}{src.suffix}"
            if dry_run:
                if log:
                    verb = "COPY" if copy_files else "MOVE"
                    log(f"DRY-RUN {verb}: {src} -> {dst_path}")
                moved_count += 1
                continue
            moved = _move_or_copy(src, dst_path, copy_files)
            bucket.files[idx] = moved
            moved_count += 1

    return sorted_root, moved_count


def _iter_valid_folders(folders: Sequence[str | Path]) -> Iterable[Path]:
    for value in folders:
        path = Path(value).expanduser()
        if path.exists() and path.is_dir():
            yield path


def sort_folders(
    folders: Sequence[str | Path],
    *,
    output_folder: str | Path | None = None,
    replace_output: bool = False,
    generic_password: str = "1234",
    make_covers: bool = False,
    copy_files: bool = False,
    dry_run: bool = False,
    auto_password: bool = False,
    log: LogFn = None,
    progress: ProgressFn = None,
) -> List[FolderSortResult]:
    generic_pw = (generic_password or "").strip() or "1234"
    valid_folders = list(_iter_valid_folders(folders))
    custom_output = Path(output_folder).expanduser() if output_folder is not None else None
    if custom_output is not None and len(valid_folders) != 1:
        raise ValueError("Custom output_folder can only be used with one source folder.")
    total = len(valid_folders)
    results: List[FolderSortResult] = []

    for idx, folder in enumerate(valid_folders, start=1):
        if progress:
            progress(idx, total, str(folder))

        if custom_output is not None and replace_output and custom_output.exists() and not dry_run:
            if custom_output.resolve() == folder.resolve():
                raise ValueError("Output folder cannot be the same as the source folder.")
            shutil.rmtree(custom_output)

        buckets = _build_buckets(folder, generic_pw, auto_password)
        file_count = sum(len(b.files) for b in buckets.values())

        if not buckets:
            results.append(
                FolderSortResult(
                    source_folder=str(folder),
                    sorted_folder=None,
                    student_count=0,
                    file_count=0,
                    moved_count=0,
                    dry_run=dry_run,
                    skipped_reason="No images found",
                )
            )
            continue

        sorted_root, moved_count = _apply_sort(
            folder,
            buckets,
            output_folder=custom_output,
            make_covers=make_covers,
            copy_files=copy_files,
            dry_run=dry_run,
            log=log,
        )

        results.append(
            FolderSortResult(
                source_folder=str(folder),
                sorted_folder=str(sorted_root),
                student_count=len(buckets),
                file_count=file_count,
                moved_count=moved_count,
                dry_run=dry_run,
            )
        )

    return results
