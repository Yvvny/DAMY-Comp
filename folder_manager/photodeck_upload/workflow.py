from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

from .bulk_upload import (
    DEFAULT_PARENT_GALLERY,
    BulkUploader,
    UserCancelled,
    copy_folders,
    create_folder,
    extract_class_name,
)
from .env_loader import load_env_file
from .photodeck_client import PhotoDeckClient


def _emit(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        try:
            log(str(message))
            return
        except Exception:
            pass
    print(message)


def _apply_pricing_profile(pricing_key: str) -> None:
    env_key = str(pricing_key or "PRICING_PROFILE").strip() or "PRICING_PROFILE"
    value = os.getenv(env_key)
    if value:
        os.environ["PRICING_PROFILE"] = value
    else:
        os.environ.pop("PRICING_PROFILE", None)


def _collect_pdf_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.pdf") if p.is_file()]
    files.sort(key=lambda p: str(p).lower())
    return files


def _count_upload_source_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    count = 0
    for entry in root.rglob("*"):
        if entry.is_file():
            count += 1
    return count


def _count_non_pdf_upload_source_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    count = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.strip().lower() != "pdfs"]
        for file_name in files:
            if not file_name.lower().endswith(".pdf"):
                count += 1
    return count


def _unique_pdf_target(root: Path, desired_name: str) -> Path:
    candidate = root / desired_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix or ".pdf"
    i = 1
    while True:
        alt = root / f"{stem} ({i}){suffix}"
        if not alt.exists():
            return alt
        i += 1


def _unique_pdf_target_in_dir(directory: Path, desired_name: str) -> Path:
    candidate = directory / desired_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix or ".pdf"
    i = 1
    while True:
        alt = directory / f"{stem} ({i}){suffix}"
        if not alt.exists():
            return alt
        i += 1


def _relocate_generated_pdfs(
    source_root: Path,
    output_root: Path,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> Path:
    source_pdfs = source_root / "PDFs"
    target_pdfs = output_root / "PDFs"
    if source_pdfs.resolve() == target_pdfs.resolve():
        return source_pdfs
    if not source_pdfs.is_dir():
        return source_pdfs

    target_pdfs.mkdir(parents=True, exist_ok=True)
    moved = 0
    for pdf_path in _collect_pdf_files(source_pdfs):
        relative_parent = pdf_path.parent.relative_to(source_pdfs)
        target_parent = target_pdfs / relative_parent
        target_parent.mkdir(parents=True, exist_ok=True)
        target_path = _unique_pdf_target_in_dir(target_parent, pdf_path.name)
        shutil.move(str(pdf_path), str(target_path))
        moved += 1

    for child in sorted(source_pdfs.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        source_pdfs.rmdir()
    except OSError:
        pass

    _emit(log, f"Moved {moved} generated PDF file(s) to: {target_pdfs}")
    return target_pdfs


def flatten_pdfs_folder(pdfs_root: str | Path) -> dict[str, int]:
    root = Path(pdfs_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    moved_count = 0
    skipped_root_count = 0

    for pdf_path in _collect_pdf_files(root):
        if pdf_path.parent == root:
            skipped_root_count += 1
            continue
        target = _unique_pdf_target(root, pdf_path.name)
        shutil.move(str(pdf_path), str(target))
        moved_count += 1

    # Remove now-empty subdirectories under PDFs.
    for child in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass

    total_files = len([p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])
    return {
        "moved_to_root": moved_count,
        "already_in_root": skipped_root_count,
        "total_pdfs": total_files,
    }


def _build_client() -> PhotoDeckClient:
    load_env_file()
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_SECRET")
    email = os.getenv("EMAIL_ADDRESS")
    password = os.getenv("PASSWORD")
    if not all([api_key, api_secret, email, password]):
        raise RuntimeError(
            "Missing PhotoDeck credentials. Set API_KEY, API_SECRET, EMAIL_ADDRESS, PASSWORD in env or .env."
        )
    return PhotoDeckClient(api_key, api_secret, email, password)


def run_stage3_create_pdfs(
    *,
    root_folder: str,
    pdf_output_root: str | None = None,
    gallery_name: str | None = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, object]:
    source_root = Path(root_folder).resolve()
    if not source_root.is_dir():
        raise RuntimeError(f"Folder does not exist: {source_root}")
    output_root = Path(pdf_output_root).resolve() if pdf_output_root else source_root
    if not output_root.is_dir():
        raise RuntimeError(f"PDF output folder does not exist: {output_root}")

    # Import lazily so optional PDF-generation deps do not affect app startup.
    from .generate_student_pdfs import main as generate_student_pdfs

    _emit(log, f"Generating PDFs from: {source_root}")
    generate_student_pdfs(str(source_root), gallery_name=gallery_name or output_root.name)
    source_pdfs_root = source_root / "PDFs"
    if not source_pdfs_root.is_dir():
        raise RuntimeError(f"PDF generation finished but PDFs folder was not created: {source_pdfs_root}")
    pdfs_root = _relocate_generated_pdfs(source_root, output_root, log=log)

    pdf_count = len(_collect_pdf_files(pdfs_root))
    _emit(log, f"PDF generation complete: {pdf_count} file(s) under {pdfs_root}")
    return {
        "root_folder": str(source_root),
        "pdfs_root": str(pdfs_root),
        "pdf_count": pdf_count,
        "pdf_output_root": str(output_root),
        "gallery_name": str(gallery_name or output_root.name).strip(),
    }


def run_stage3_bulk_upload(
    *,
    root_folder: str,
    picture_day_id: str,
    gallery_name: str | None = None,
    pricing_key: str = "PRICING_PROFILE",
    max_workers: int = 4,
    on_existing_gallery=None,
    on_new_gallery_name=None,
    log: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    root_use_folder_name_password: bool = False,
    nested_use_folder_name_password: bool = True,
) -> dict[str, object]:
    root = Path(root_folder).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Folder does not exist: {root}")

    upload_prefix = str(picture_day_id or "").strip()
    if not upload_prefix:
        raise RuntimeError("Picture Day ID is required for bulk upload.")

    source_file_count = _count_non_pdf_upload_source_files(root)
    if source_file_count <= 0:
        raise RuntimeError(f"No files found to upload under: {root}")

    _apply_pricing_profile(pricing_key)
    client = _build_client()
    uploader = BulkUploader(client, max_workers=max_workers, filename_prefix=upload_prefix, cancel_event=cancel_event)
    deferred_cover_jobs: list[tuple[str, str, Optional[str], Optional[str]]] = []
    uploader_closed = False
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise UserCancelled("Upload cancelled by user.")
        root_gallery_name = str(gallery_name or root.name).strip() or root.name
        root_gallery_actual_name: list[str] = []
        root_gallery_uuid = create_folder(
            client,
            root_gallery_name,
            DEFAULT_PARENT_GALLERY,
            on_existing_gallery=on_existing_gallery,
            on_new_gallery_name=on_new_gallery_name,
            actual_name_out=root_gallery_actual_name,
            use_folder_name_password=root_use_folder_name_password,
        )
        if root_gallery_actual_name:
            root_gallery_name = root_gallery_actual_name[0]
        _emit(log, f"Starting bulk upload from: {root}")
        for item in sorted(os.listdir(root), reverse=True):
            if cancel_event is not None and cancel_event.is_set():
                raise UserCancelled("Upload cancelled by user.")
            source_path = os.path.join(root, item)
            if os.path.isdir(source_path):
                if item.strip().lower() == "pdfs":
                    # Stage-3 Upload should not upload generated packet PDFs.
                    continue
                copy_folders(
                    source_path,
                    root_gallery_uuid,
                    client,
                    uploader,
                    on_existing_gallery=on_existing_gallery,
                    on_new_gallery_name=on_new_gallery_name,
                    cancel_event=cancel_event,
                    use_folder_name_password=nested_use_folder_name_password,
                    deferred_cover_jobs=deferred_cover_jobs,
                )
            else:
                if item.lower().endswith(".pdf"):
                    continue
                class_name = extract_class_name(os.path.basename(root))
                name_scope = os.path.basename(root)
                if item.strip().lower().startswith("#cover"):
                    deferred_cover_jobs.append((source_path, root_gallery_uuid, class_name, name_scope))
                else:
                    uploader.add_job(source_path, root_gallery_uuid, class_name, name_scope)
        uploader_closed = True
        uploader.close()
    except Exception:
        if not uploader_closed:
            try:
                uploader.close()
            except Exception as close_exc:
                _emit(log, f"[WARN] Cleanup after failed upload also reported: {close_exc}")
        raise

    for cover_path, gallery_uuid, class_name, name_scope in deferred_cover_jobs:
        if cancel_event is not None and cancel_event.is_set():
            raise UserCancelled("Upload cancelled by user.")
        _emit(log, f"Uploading cover last: {Path(cover_path).name}")
        uploader.upload_now(cover_path, gallery_uuid, class_name, name_scope)
    uploader.raise_for_failures()

    return {
        "source_root": str(root),
        "picture_day_id": upload_prefix,
        "scheduled_file_count": uploader.scheduled_count,
        "uploaded_count": uploader.uploaded_count,
        "source_file_count": source_file_count,
        "gallery_name": root_gallery_name,
        "gallery_uuid": root_gallery_uuid,
    }


def upload_flat_pdfs_to_gallery(
    *,
    pdfs_root: str | Path,
    gallery_name: str,
    picture_day_id: str,
    pricing_key: str = "PRICING_PROFILE",
    parent_gallery_uuid: str = DEFAULT_PARENT_GALLERY,
    max_workers: int = 4,
    on_existing_gallery=None,
    on_new_gallery_name=None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, object]:
    pdfs_dir = Path(pdfs_root).resolve()
    if not pdfs_dir.is_dir():
        raise RuntimeError(f"PDF folder does not exist: {pdfs_dir}")

    pdf_files = [p for p in sorted(pdfs_dir.iterdir(), key=lambda p: p.name.lower()) if p.is_file() and p.suffix.lower() == ".pdf"]
    if not pdf_files:
        raise RuntimeError(f"No PDF files found in: {pdfs_dir}")

    _apply_pricing_profile(pricing_key)
    client = _build_client()
    upload_prefix = str(picture_day_id or "").strip()
    if not upload_prefix:
        upload_prefix = "upload"

    _emit(log, f"Creating/reusing PhotoDeck gallery: {gallery_name}")
    gallery_actual_name: list[str] = []
    gallery_uuid = create_folder(
        client,
        str(gallery_name or "").strip() or pdfs_dir.parent.name or "Upload",
        parent_gallery_uuid,
        on_existing_gallery=on_existing_gallery,
        on_new_gallery_name=on_new_gallery_name,
        actual_name_out=gallery_actual_name,
    )
    resolved_gallery_name = gallery_actual_name[0] if gallery_actual_name else str(gallery_name or "").strip()

    _emit(log, f"Uploading {len(pdf_files)} PDF file(s) to gallery {gallery_uuid}...")
    uploader = BulkUploader(client, max_workers=max_workers, filename_prefix=upload_prefix)
    try:
        for pdf_path in pdf_files:
            uploader.add_job(str(pdf_path), gallery_uuid, None)
    finally:
        uploader.close()

    return {
        "gallery_uuid": gallery_uuid,
        "gallery_name": resolved_gallery_name,
        "uploaded_count": len(pdf_files),
        "pdfs_root": str(pdfs_dir),
    }


def run_stage3_upload_pipeline(
    *,
    root_folder: str,
    picture_day_id: str,
    pdf_output_root: str | None = None,
    pricing_key: str = "PRICING_PROFILE",
    max_workers: int = 4,
    on_existing_gallery=None,
    on_new_gallery_name=None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, object]:
    source_root = Path(root_folder).resolve()
    if not source_root.is_dir():
        raise RuntimeError(f"Folder does not exist: {source_root}")
    output_root = Path(pdf_output_root).resolve() if pdf_output_root else source_root
    if not output_root.is_dir():
        raise RuntimeError(f"PDF output folder does not exist: {output_root}")

    # Import lazily so optional PDF-generation deps do not affect app startup.
    from .generate_student_pdfs import main as generate_student_pdfs

    _emit(log, f"Generating PDFs from: {source_root}")
    generate_student_pdfs(str(source_root), gallery_name=output_root.name)
    source_pdfs_root = source_root / "PDFs"
    if not source_pdfs_root.is_dir():
        raise RuntimeError(f"PDF generation finished but PDFs folder was not created: {source_pdfs_root}")
    pdfs_root = _relocate_generated_pdfs(source_root, output_root, log=log)

    flatten_stats = flatten_pdfs_folder(pdfs_root)
    _emit(
        log,
        (
            "PDF flatten complete: "
            f"{flatten_stats['total_pdfs']} total, "
            f"{flatten_stats['moved_to_root']} moved from subfolders."
        ),
    )

    upload_result = upload_flat_pdfs_to_gallery(
        pdfs_root=pdfs_root,
        gallery_name=output_root.name,
        picture_day_id=picture_day_id,
        pricing_key=pricing_key,
        max_workers=max_workers,
        on_existing_gallery=on_existing_gallery,
        on_new_gallery_name=on_new_gallery_name,
        log=log,
    )

    return {
        "root_folder": str(source_root),
        "pdfs_root": str(pdfs_root),
        "pdf_output_root": str(output_root),
        "picture_day_id": str(picture_day_id or "").strip(),
        "flatten": flatten_stats,
        **upload_result,
    }


__all__ = [
    "UserCancelled",
    "flatten_pdfs_folder",
    "run_stage3_bulk_upload",
    "run_stage3_create_pdfs",
    "run_stage3_upload_pipeline",
    "upload_flat_pdfs_to_gallery",
]
