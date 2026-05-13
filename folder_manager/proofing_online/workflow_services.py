from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from folder_manager.proof_sorter import sort_folders

from .path_resolver import is_same_or_inside_path, is_same_path
from .workflow_config import strip_stage_prefix_text

PICTURE_DAY_ID_RE = re.compile(r"\b[PH]\d{7,8}\b", re.IGNORECASE)


def sanitize_folder_name(value: str) -> str:
    sanitized = re.sub(r'[\\/:*?"<>|]', "_", str(value or "").strip())
    return sanitized or "Folder"


def stage2_proof_output_name(disk_name: str) -> str:
    name = str(disk_name or "").strip()
    pid_match = PICTURE_DAY_ID_RE.search(name)
    if not pid_match:
        return sanitize_folder_name(f"{name} Proof")

    pid = pid_match.group(0).upper()
    prefix = name[:pid_match.start()].strip()
    date_part = ""
    school_part = prefix
    date_match = re.match(r"^(\d{6})\s*(.*)$", prefix)
    if date_match:
        date_part = date_match.group(1).strip()
        school_part = date_match.group(2).strip()

    parts = [part for part in (date_part, school_part, "Proof", pid) if part]
    return sanitize_folder_name(" ".join(parts))


def stage2_sort_output_names(disk_name: str) -> set[str]:
    name = str(disk_name or "").strip()
    names = {stage2_proof_output_name(name), "sorted"}
    pid_match = PICTURE_DAY_ID_RE.search(name)
    if not pid_match:
        names.add(sanitize_folder_name(f"{name} Proofs"))
        return {item for item in names if item}

    pid = pid_match.group(0).upper()
    prefix = name[:pid_match.start()].strip()
    date_part = ""
    school_part = prefix
    date_match = re.match(r"^(\d{6})\s*(.*)$", prefix)
    if date_match:
        date_part = date_match.group(1).strip()
        school_part = date_match.group(2).strip()

    for label in ("Proof", "Proofs"):
        parts = [part for part in (date_part, school_part, label, pid) if part]
        names.add(sanitize_folder_name(" ".join(parts)))
    return {item for item in names if item}


def infer_stage3_upload_id(disk_name: str) -> str:
    token = str(disk_name or "").strip()
    pid_match = PICTURE_DAY_ID_RE.search(token)
    if pid_match:
        return pid_match.group(0).upper()
    date_match = re.match(r"^\s*(\d{6,8})\b", token)
    if date_match:
        return date_match.group(1)
    return datetime.now().strftime("%y%m%d")


def stage3_gallery_name_from_paths(work_root: str, folder_path: str, disk_name: str) -> str:
    work_name = os.path.basename(str(work_root or "").rstrip("/\\"))
    if work_name and work_name.lower() != "sorted":
        return strip_stage_prefix_text(work_name)
    folder_name = os.path.basename(str(folder_path or "").rstrip("/\\"))
    if folder_name:
        return strip_stage_prefix_text(folder_name)
    return strip_stage_prefix_text(disk_name)


def extract_school_name(display_name: str) -> str:
    text = str(display_name or "").strip()
    text = re.sub(r"^\d{6}\s+", "", text)
    text = re.sub(r"\s+P\d{8,}\b.*$", "", text).strip()
    return text or str(display_name or "").strip()


def first_email_from_text(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    match = re.search(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", text, flags=re.IGNORECASE)
    return (match.group(0).strip() if match else "").strip()


@dataclass(frozen=True)
class Stage2SortPlan:
    disk_name: str
    job_folder: str
    source_folder: str
    output_folder: str
    temp_output_folder: str
    existing_output_paths: Tuple[str, ...]
    source_will_be_deleted: bool


@dataclass(frozen=True)
class Stage2SortRunResult:
    sort_results: object
    replaced_outputs: Tuple[str, ...]
    source_removed: bool
    cleanup_warnings: Tuple[str, ...]


@dataclass(frozen=True)
class Stage4SchoolEmailDraft:
    to_email: str
    subject: str
    body_text: str
    pdf_root: str
    pdf_files: Tuple[str, ...]
    school_name: str


@dataclass(frozen=True)
class Stage3UploadPlan:
    disk_name: str
    folder_path: str
    work_root: str
    picture_day_id: str
    gallery_name: str


@dataclass(frozen=True)
class Stage3UploadSummary:
    source_root: str
    uploaded_count: int
    gallery_name: str
    gallery_uuid: str


@dataclass(frozen=True)
class Stage3PdfSummary:
    pdfs_path: str
    pdf_count: int


def build_stage3_upload_plan(disk_name: str, folder_path: str, work_root: str) -> Stage3UploadPlan:
    return Stage3UploadPlan(
        disk_name=str(disk_name or "").strip(),
        folder_path=str(folder_path or "").strip(),
        work_root=str(work_root or "").strip(),
        picture_day_id=infer_stage3_upload_id(disk_name),
        gallery_name=stage3_gallery_name_from_paths(work_root, folder_path, disk_name),
    )


def summarize_stage3_upload_result(result: object, plan: Stage3UploadPlan) -> Stage3UploadSummary:
    result_map = result if isinstance(result, Mapping) else {}
    return Stage3UploadSummary(
        source_root=str(result_map.get("source_root") or plan.work_root),
        uploaded_count=int(result_map.get("uploaded_count") or result_map.get("scheduled_file_count") or 0),
        gallery_name=str(result_map.get("gallery_name") or plan.disk_name).strip(),
        gallery_uuid=str(result_map.get("gallery_uuid") or "").strip(),
    )


def summarize_stage3_pdf_result(result: object, folder_path: str) -> Stage3PdfSummary:
    result_map = result if isinstance(result, Mapping) else {}
    return Stage3PdfSummary(
        pdfs_path=str(result_map.get("pdfs_root") or os.path.join(folder_path, "PDFs")),
        pdf_count=int(result_map.get("pdf_count") or 0),
    )


def build_stage2_sort_plan(disk_name: str, job_folder: str, source_folder: str) -> Stage2SortPlan:
    output_name = stage2_proof_output_name(disk_name)
    output_folder = os.path.join(job_folder, output_name)
    temp_output_folder = os.path.join(job_folder, f".{output_name}.sorting_tmp")
    existing_output_paths = tuple(
        os.path.join(job_folder, name)
        for name in sorted(stage2_sort_output_names(disk_name))
        if os.path.isdir(os.path.join(job_folder, name))
    )
    return Stage2SortPlan(
        disk_name=str(disk_name or "").strip(),
        job_folder=str(job_folder or "").strip(),
        source_folder=str(source_folder or "").strip(),
        output_folder=output_folder,
        temp_output_folder=temp_output_folder,
        existing_output_paths=existing_output_paths,
        source_will_be_deleted=not is_same_path(source_folder, job_folder),
    )


def execute_stage2_sort_plan(
    plan: Stage2SortPlan,
    *,
    sort_runner: Callable[..., Sequence[object]] = sort_folders,
) -> Stage2SortRunResult:
    if os.path.isdir(plan.temp_output_folder):
        shutil.rmtree(plan.temp_output_folder)

    sort_results = list(
        sort_runner(
            [plan.source_folder],
            output_folder=plan.temp_output_folder,
            replace_output=True,
            generic_password=os.getenv("DAMY_PROOFING_GENERIC_PASSWORD", ""),
            make_covers=True,
            copy_files=True,
            dry_run=False,
            auto_password=False,
        )
    )
    moved = sum(int(getattr(row, "moved_count", 0) or 0) for row in sort_results)
    expected = sum(int(getattr(row, "file_count", 0) or 0) for row in sort_results)
    if expected <= 0 or moved < expected:
        raise RuntimeError("New sorted output was not completed. Old output and source were not changed.")
    if not os.path.isdir(plan.temp_output_folder):
        raise RuntimeError("New sorted output folder was not created. Old output and source were not changed.")

    backup_suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    backups: List[Tuple[str, str]] = []
    replaced_outputs: List[str] = []
    try:
        for old_path in plan.existing_output_paths:
            if not os.path.isdir(old_path):
                continue
            backup_path = f"{old_path}.old_sort_backup_{backup_suffix}"
            counter = 2
            while os.path.exists(backup_path):
                backup_path = f"{old_path}.old_sort_backup_{backup_suffix}_{counter}"
                counter += 1
            shutil.move(old_path, backup_path)
            backups.append((old_path, backup_path))
            replaced_outputs.append(old_path)

        shutil.move(plan.temp_output_folder, plan.output_folder)
    except Exception:
        if os.path.isdir(plan.output_folder):
            shutil.rmtree(plan.output_folder, ignore_errors=True)
        for original_path, backup_path in reversed(backups):
            if os.path.exists(backup_path) and not os.path.exists(original_path):
                shutil.move(backup_path, original_path)
        raise

    cleanup_warnings: List[str] = []
    for _original_path, backup_path in backups:
        if os.path.isdir(backup_path):
            try:
                shutil.rmtree(backup_path)
            except Exception as exc:  # pylint: disable=broad-except
                cleanup_warnings.append(f"Could not delete old output backup {backup_path}: {exc}")

    source_removed = False
    if plan.source_will_be_deleted and os.path.isdir(plan.source_folder):
        try:
            shutil.rmtree(plan.source_folder)
            source_removed = True
        except Exception as exc:  # pylint: disable=broad-except
            cleanup_warnings.append(f"Could not delete source folder {plan.source_folder}: {exc}")
    elif plan.source_will_be_deleted and not os.path.exists(plan.source_folder):
        source_removed = True

    return Stage2SortRunResult(
        sort_results=sort_results,
        replaced_outputs=tuple(replaced_outputs),
        source_removed=source_removed,
        cleanup_warnings=tuple(cleanup_warnings),
    )


def stage2_source_conflicts_existing_output(plan: Stage2SortPlan) -> bool:
    return any(is_same_or_inside_path(plan.source_folder, old_path) for old_path in plan.existing_output_paths)


def parent_delivery_pdf_status(job_folder: str, collect_pdf_files: Callable[[str], tuple[str, List[str]]]) -> tuple[bool, str, int]:
    pdf_root = os.path.join(job_folder, "PDFs")
    if os.path.isdir(pdf_root):
        checked_root, pdf_files = collect_pdf_files(pdf_root)
        return bool(pdf_files), checked_root, len(pdf_files)
    return False, pdf_root, 0


def contact_email_for_row(row: object, parse_note_fields: Callable[[object], tuple[object, object, object]]) -> str:
    contact_email = (getattr(row, "contact_email", None) or "").strip()
    if contact_email:
        return contact_email
    _name, parsed_email, _phone = parse_note_fields(getattr(row, "note", None))
    return str(parsed_email or "").strip()


def build_stage4_school_email_draft(
    *,
    row: object,
    disk_name: str,
    folder_path: str,
    display_name: str,
    collect_stage3_pdf_files: Callable[[str, str], tuple[str, List[str]]],
    parse_note_fields: Callable[[object], tuple[object, object, object]],
) -> Stage4SchoolEmailDraft:
    contact_email = contact_email_for_row(row, parse_note_fields)
    to_email = first_email_from_text(contact_email)
    pdf_root, pdf_files = collect_stage3_pdf_files(disk_name, folder_path)
    school_name = extract_school_name(display_name or getattr(row, "display_name", "") or disk_name)
    return Stage4SchoolEmailDraft(
        to_email=to_email,
        subject=f"{school_name} - Proofs",
        body_text=f"Hello {school_name},\n\nAttached are your proof PDFs.\n\nThank you.",
        pdf_root=pdf_root,
        pdf_files=tuple(pdf_files),
        school_name=school_name,
    )
