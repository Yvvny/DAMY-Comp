from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple

PARENT_DELIVERY_ASSETS_DIRNAME = "Parent Delivery Assets"


def natural_sort_key(text: str):
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r"(\d+)", text)]


def _normalize_dir_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def find_matching_subdir(base_folder: str, target_name: str) -> str:
    expected_path = os.path.join(base_folder, target_name)
    if not os.path.exists(base_folder):
        return expected_path

    normalized_target = _normalize_dir_token(target_name)
    for entry in os.listdir(base_folder):
        full_path = os.path.join(base_folder, entry)
        if not os.path.isdir(full_path):
            continue
        if _normalize_dir_token(entry) == normalized_target:
            return full_path
    return expected_path


def is_same_or_inside_path(path: str, parent: str) -> bool:
    try:
        child_path = os.path.normcase(os.path.abspath(path))
        parent_path = os.path.normcase(os.path.abspath(parent))
        return child_path == parent_path or os.path.commonpath([child_path, parent_path]) == parent_path
    except Exception:
        return False


def is_same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except Exception:
        return False


class ProofingPathResolver:
    def __init__(self, base_dir: str, *, source_base_dir: str = "", domain: str = "proofing"):
        self.base_dir = str(base_dir or "").strip()
        self.source_base_dir = str(source_base_dir or "").strip()
        self.domain = str(domain or "proofing").strip().lower() or "proofing"

    def runtime_workspace_root(self) -> str:
        domain_key = re.sub(r"[^a-z0-9_-]+", "_", self.domain) or "proofing"
        return os.path.join(self.base_dir, "_workflow_runtime", domain_key)

    def resolve_nested_stage_folder_path(self, root_dir: str, stage_folder_name: str, disk_name: str) -> Optional[str]:
        root = str(root_dir or "").strip()
        target_name = str(disk_name or "").strip()
        if not root or not target_name or not os.path.isdir(root):
            return None

        direct_stage = find_matching_subdir(root, stage_folder_name)
        if os.path.isdir(direct_stage):
            candidate = find_matching_subdir(direct_stage, target_name)
            if os.path.isdir(candidate):
                return candidate

        for entry in os.listdir(root):
            entry_path = os.path.join(root, entry)
            if not os.path.isdir(entry_path):
                continue
            stage_path = find_matching_subdir(entry_path, stage_folder_name)
            if not os.path.isdir(stage_path):
                continue
            candidate = find_matching_subdir(stage_path, target_name)
            if os.path.isdir(candidate):
                return candidate
        return None

    def resolve_existing_folder_path_for_open(self, disk_name: str) -> Optional[str]:
        name = str(disk_name or "").strip()
        if not name:
            return None
        candidates = [
            os.path.join(self.base_dir, name),
            os.path.join(self.base_dir, "cancel", name),
        ]
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate

        for root in (self.base_dir, os.path.join(self.base_dir, "cancel")):
            nested = self.resolve_nested_stage_folder_path(root, "3. Edit", name)
            if nested:
                return nested

        if self.source_base_dir:
            candidates = [
                os.path.join(self.source_base_dir, name),
                os.path.join(self.source_base_dir, "cancel", name),
            ]
            for candidate in candidates:
                if os.path.isdir(candidate):
                    return candidate
            for root in (self.source_base_dir, os.path.join(self.source_base_dir, "cancel")):
                nested = self.resolve_nested_stage_folder_path(root, "3. Edit", name)
                if nested:
                    return nested
        return None

    def stage3_work_root(self, folder_path: str) -> str:
        root = str(folder_path or "").strip()
        if not root:
            return root
        sorted_path = os.path.join(root, "sorted")
        if os.path.isdir(sorted_path):
            try:
                with os.scandir(sorted_path) as it:
                    for entry in it:
                        if entry.is_dir() or entry.is_file():
                            return sorted_path
            except OSError:
                pass
        return root

    def collect_pdf_files_from_path(self, raw_path: str) -> tuple[str, List[str]]:
        pdf_path = str(raw_path or "").strip()
        if not pdf_path:
            return "", []
        if os.path.isfile(pdf_path):
            if pdf_path.lower().endswith(".pdf"):
                return os.path.dirname(pdf_path), [pdf_path]
            return os.path.dirname(pdf_path), []
        if not os.path.isdir(pdf_path):
            return pdf_path, []

        pdf_files: List[str] = []
        for root, _dirs, files in os.walk(pdf_path):
            if PARENT_DELIVERY_ASSETS_DIRNAME.casefold() in {part.casefold() for part in Path(root).parts}:
                continue
            for file_name in files:
                if file_name.lower().endswith(".pdf"):
                    pdf_files.append(os.path.join(root, file_name))
        pdf_files.sort(key=lambda p: natural_sort_key(os.path.basename(p)))
        return pdf_path, pdf_files

    def stage3_pdf_candidates(
        self,
        disk_name: str,
        folder_path: str,
        *,
        read_saved_link: Optional[Callable[[str], str]] = None,
    ) -> List[str]:
        candidates: List[str] = []
        if read_saved_link is not None:
            saved_path = str(read_saved_link(disk_name) or "").strip()
            if saved_path:
                candidates.append(saved_path)
        primary_pdf_root = os.path.join(folder_path, "PDFs")
        candidates.append(primary_pdf_root)
        work_root = self.stage3_work_root(folder_path)
        fallback_pdf_root = os.path.join(work_root, "PDFs")
        if fallback_pdf_root not in candidates:
            candidates.append(fallback_pdf_root)
        return candidates

    def collect_stage3_pdf_files(
        self,
        disk_name: str,
        folder_path: str,
        *,
        read_saved_link: Optional[Callable[[str], str]] = None,
    ) -> Tuple[str, List[str]]:
        first_candidate = ""
        for candidate in self.stage3_pdf_candidates(disk_name, folder_path, read_saved_link=read_saved_link):
            if not first_candidate:
                first_candidate = candidate
            pdf_root, pdf_files = self.collect_pdf_files_from_path(candidate)
            if pdf_files:
                return pdf_root, pdf_files
        return first_candidate or os.path.join(folder_path, "PDFs"), []
