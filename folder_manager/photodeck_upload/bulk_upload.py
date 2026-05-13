#!/usr/bin/env python3
import argparse
import os
import queue
import threading
import xml.etree.ElementTree as ET
from typing import Callable, List, Optional, Tuple

from requests.exceptions import HTTPError

try:
    import tkinter as tk
    from tkinter import filedialog, simpledialog, messagebox
except Exception:  # pragma: no cover - optional dependency in packaged runtimes
    tk = None
    filedialog = None
    simpledialog = None
    messagebox = None

if __package__ is None or __package__ == "":
    import sys
    from pathlib import Path

    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from folder_manager.photodeck_upload.photodeck_client import (
        PhotoDeckClient,
        default_optional_post_params,
        default_optional_put_params,
    )
    from folder_manager.photodeck_upload.env_loader import load_env_file
    from folder_manager.photodeck_upload.links import photodeck_url_path, strip_trailing_gallery_password, trailing_gallery_password
else:
    from .photodeck_client import (
        PhotoDeckClient,
        default_optional_post_params,
        default_optional_put_params,
    )
    from .env_loader import load_env_file
    from .links import photodeck_url_path, strip_trailing_gallery_password, trailing_gallery_password


DEFAULT_PARENT_GALLERY = "94a3358f-8233-4995-b130-8ae187713a4d"
URL_NAME = "theschoolphotocompany_photodeck_com"

load_env_file()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
PASSWORD = os.getenv("PASSWORD")


class UserCancelled(Exception):
    """Raised when the user cancels an action from a dialog."""


class UploadFailuresError(Exception):
    def __init__(self, failed_count: int, detail: str) -> None:
        self.failed_count = int(failed_count)
        self.detail = str(detail or "").strip()
        super().__init__(f"{self.failed_count} PhotoDeck upload file(s) failed. Open Details for the file list.")


ExistingGalleryPrompt = Callable[[str, str, Optional[str]], str]
NewGalleryNamePrompt = Callable[[str, Optional[str]], Optional[str]]


def extract_class_name(folder_name: str) -> Optional[str]:
    start = folder_name.find('[')
    if start == -1:
        return None
    end = folder_name.find(']', start + 1)
    if end == -1:
        return None
    class_name = folder_name[start + 1:end].strip()
    return class_name or None


class BulkUploader:
    def __init__(
        self,
        client: PhotoDeckClient,
        max_workers: int = 4,
        filename_prefix: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.client = client
        self.base_optional_post = default_optional_post_params()
        self.base_optional_put = default_optional_put_params()
        self.max_workers = max_workers
        self.filename_prefix = (filename_prefix or "").strip()
        self.cancel_event = cancel_event or threading.Event()
        self._queue: "queue.Queue[Optional[Tuple[str, str, Optional[str], Optional[str]]]]" = queue.Queue()
        self._lock = threading.Lock()
        self.scheduled_count = 0
        self.uploaded_files: List[Tuple[str, str]] = []
        self.failed_uploads: List[Tuple[str, str]] = []
        self._workers = [
            threading.Thread(target=self._worker, name=f"photodeck-upload-{i}", daemon=True)
            for i in range(max_workers)
        ]

        for worker in self._workers:
            worker.start()

    def add_job(
        self,
        file_path: str,
        gallery_uuid: str,
        class_name: Optional[str] = None,
        name_scope: Optional[str] = None,
    ) -> None:
        if self.cancel_event.is_set():
            raise UserCancelled("Upload cancelled by user.")
        with self._lock:
            self.scheduled_count += 1
        self._queue.put((file_path, gallery_uuid, class_name, name_scope))

    def upload_now(
        self,
        file_path: str,
        gallery_uuid: str,
        class_name: Optional[str] = None,
        name_scope: Optional[str] = None,
    ) -> None:
        if self.cancel_event.is_set():
            raise UserCancelled("Upload cancelled by user.")
        with self._lock:
            self.scheduled_count += 1
        try:
            uploaded_name = self._upload_file(file_path, gallery_uuid, class_name, name_scope)
        except Exception as exc:
            self._record_failure(file_path, exc)
            raise
        self._record_success(file_path, uploaded_name)

    def close(self) -> None:
        if self.cancel_event.is_set():
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._queue.task_done()
        self._queue.join()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join()
        self.raise_for_failures()

    @property
    def uploaded_count(self) -> int:
        with self._lock:
            return len(self.uploaded_files)

    def _record_success(self, file_path: str, uploaded_name: str) -> None:
        with self._lock:
            self.uploaded_files.append((file_path, uploaded_name))

    def _record_failure(self, file_path: str, error: Exception) -> None:
        with self._lock:
            self.failed_uploads.append((file_path, str(error)))

    def raise_for_failures(self) -> None:
        with self._lock:
            failures = list(self.failed_uploads)
        if not failures:
            return
        preview = "\n".join(f"- {path}: {error}" for path, error in failures[:20])
        if len(failures) > 20:
            preview += f"\n- ... {len(failures) - 20} more"
        raise UploadFailuresError(len(failures), preview)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break

            file_path, gallery_uuid, class_name, name_scope = item
            try:
                uploaded_name = self._upload_file(file_path, gallery_uuid, class_name, name_scope)
                self._record_success(file_path, uploaded_name)
            except Exception as exc:
                self._record_failure(file_path, exc)
                print(f"Failed to upload {file_path}: {exc}")
            finally:
                self._queue.task_done()

    @staticmethod
    def _filename_part(value: Optional[str]) -> str:
        cleaned = strip_trailing_gallery_password(str(value or ""))
        cleaned = cleaned.replace("[", " ").replace("]", " ")
        cleaned = cleaned.replace("#", "cover")
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in cleaned)
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned.strip("_")

    def _upload_file(
        self,
        file_path: str,
        gallery_uuid: str,
        class_name: Optional[str] = None,
        name_scope: Optional[str] = None,
    ) -> str:
        initial_xml: Optional[ET.Element] = None
        base_name = os.path.basename(file_path)
        prefixed_name = base_name
        try:
            if self.cancel_event.is_set():
                return
            if self.filename_prefix:
                name_parts = [self.filename_prefix]
                if class_name:
                    cleaned_class = self._filename_part(class_name)
                    if cleaned_class:
                        name_parts.append(cleaned_class)
                cleaned_scope = self._filename_part(name_scope)
                if cleaned_scope and cleaned_scope not in name_parts:
                    name_parts.append(cleaned_scope)
                prefixed_name = "_".join(name_parts + [base_name])
                print(prefixed_name)

            optional_post = dict(self.base_optional_post)
            optional_put = dict(self.base_optional_put)
            optional_put["media[publish_to_galleries][]"] = gallery_uuid

            try:
                initial_xml = self.client.create_media(
                    file_path,
                    optional_post,
                    file_name_override=prefixed_name,
                )
            except Exception as exc:
                raise RuntimeError(f"Create media failed for {prefixed_name}: {exc}") from exc
            try:
                self.client.upload_file_to_storage(initial_xml, file_path, file_name_override=prefixed_name)
            except Exception as exc:
                raise RuntimeError(f"Storage upload failed for {prefixed_name}: {exc}") from exc
            try:
                self.client.finalize_media(initial_xml, file_path, optional_put)
            except Exception as exc:
                raise RuntimeError(f"Finalize media failed for {prefixed_name}: {exc}") from exc
            print(f"Uploaded {prefixed_name} to gallery {gallery_uuid}")
            return prefixed_name
        except Exception:
            if initial_xml is not None:
                try:
                    self.client.delete_media(initial_xml)
                except Exception as cleanup_exc:
                    print(f"Failed to delete incomplete upload for {file_path}: {cleanup_exc}")
            raise


def find_email_or_phone_substring_and_remainder(input_string: str) -> Tuple[Optional[str], str]:
    import re

    first_underscore = input_string.find('_')
    if first_underscore != -1:
        second_underscore = input_string.find('_', first_underscore + 1)
        if second_underscore != -1 and second_underscore > first_underscore + 1:
            password = input_string[first_underscore + 1:second_underscore].strip()
            remainder = (input_string[:first_underscore] + input_string[second_underscore:])
            while '__' in remainder:
                remainder = remainder.replace('__', '_')
            remainder = remainder.strip('_ ').strip()
            if password:
                return password, remainder or input_string

    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    number_pattern = r'\b\d+\b'

    email_match = re.search(email_pattern, input_string)
    if email_match:
        email = email_match.group(0)
        username = email.split('@')[0].lower()
        remainder = input_string.replace(email, '').strip()
        return username, remainder

    phone_match = re.search(number_pattern, input_string)
    if phone_match:
        phone_number = phone_match.group(0)
        last_4_digits = phone_number[-4:]
        remainder = input_string.replace(phone_number, '').strip()
        return last_4_digits, remainder

    return None, input_string


def _strip_password_token_from_name(input_string: str) -> str:
    raw = str(input_string or "").strip()
    first_underscore = raw.find('_')
    if first_underscore != -1:
        second_underscore = raw.find('_', first_underscore + 1)
        if second_underscore != -1 and second_underscore > first_underscore + 1:
            remainder = raw[:first_underscore] + raw[second_underscore:]
            while '__' in remainder:
                remainder = remainder.replace('__', '_')
            cleaned = remainder.strip('_ ').strip()
            if cleaned:
                return cleaned
    return raw


def _derive_gallery_name_and_password(folder_name: str, use_folder_name_password: bool) -> Tuple[Optional[str], str]:
    raw_name = str(folder_name or "").strip()
    if use_folder_name_password:
        password = trailing_gallery_password(raw_name)
        base_name = strip_trailing_gallery_password(raw_name) if password else raw_name
        if not password:
            password, base_name = find_email_or_phone_substring_and_remainder(raw_name)
    else:
        password, base_name = None, _strip_password_token_from_name(raw_name)
    gallery_name = str(base_name or raw_name).strip() or raw_name
    return password, gallery_name


def find_existing_folder(client: PhotoDeckClient, parent_uuid: str, folder_name: str) -> Tuple[Optional[str], Optional[str]]:
    endpoint = f"https://api.photodeck.com/websites/{URL_NAME}/galleries/{parent_uuid}/subgalleries.xml"
    response = client.api_call('GET', endpoint, params={'filter[name]': folder_name})
    root = ET.fromstring(response.text)
    normalized_target = folder_name.strip().lower()

    for gallery in root.findall('.//gallery'):
        name = (gallery.findtext('name') or gallery.findtext('title') or '').strip().lower()
        if name == normalized_target:
            uuid = gallery.findtext('gallery-uuid') or gallery.findtext('uuid')
            return uuid, name

    return None, None


def find_existing_folder_any_name(
    client: PhotoDeckClient,
    parent_uuid: str,
    names: List[str],
) -> Tuple[Optional[str], Optional[str]]:
    seen: set[str] = set()
    for candidate in names:
        normalized = str(candidate or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        uuid, actual = find_existing_folder(client, parent_uuid, normalized)
        if uuid:
            return uuid, actual
    return None, None


def _gallery_fields(gallery_name: str, password: Optional[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    clean_name = str(gallery_name or "").strip()
    if clean_name:
        fields["gallery[name]"] = clean_name
    url_path = photodeck_url_path(gallery_name)
    if url_path:
        fields["gallery[url_path]"] = url_path
    if password:
        fields["gallery[password]"] = password
    return fields


def _sync_existing_gallery_metadata(
    client: PhotoDeckClient,
    gallery_uuid: str,
    gallery_name: str,
    password: Optional[str],
) -> None:
    fields = _gallery_fields(gallery_name, password)
    if not fields:
        return
    client.update_gallery(URL_NAME, gallery_uuid, fields)


def prompt_existing_gallery_action(folder_name: str, existing_uuid: str, error_text: Optional[str] = None) -> str:
    """Ask whether to reuse an existing gallery, create new, or cancel."""
    if tk is None or messagebox is None:
        raise RuntimeError("tkinter is unavailable for prompt dialogs.")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    extra = ""
    snippet = (error_text or "").strip()
    if snippet:
        snippet = snippet.replace("\r", " ").replace("\n", " ")
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        extra = f"\n\nPhotoDeck response:\n{snippet}"

    msg = (
        f"A gallery named '{folder_name}' already exists.\n"
        "Yes = upload into the existing gallery.\n"
        "No = create a new gallery instead.\n"
        "Cancel = stop the upload."
        f"{extra}"
    )
    choice = messagebox.askyesnocancel("Gallery Already Exists", msg, parent=root)
    root.destroy()

    if choice is None:
        return "cancel"
    return "use_existing" if choice else "create_new"


def prompt_new_gallery_name(default_name: str, error_text: Optional[str] = None) -> Optional[str]:
    """Prompt for a new gallery name, defaulting to the provided one."""
    if tk is None or simpledialog is None:
        raise RuntimeError("tkinter is unavailable for prompt dialogs.")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    prompt = "Enter a name for the new gallery:"
    if error_text:
        prompt += f"\n\nPrevious error:\n{error_text}"

    new_name = simpledialog.askstring("New Gallery Name", prompt, initialvalue=default_name, parent=root)
    root.destroy()
    if new_name is None:
        return None
    new_name = new_name.strip()
    return new_name or default_name


def create_folder(
    client: PhotoDeckClient,
    folder_name: str,
    parent_uuid: str,
    on_existing_gallery: Optional[ExistingGalleryPrompt] = None,
    on_new_gallery_name: Optional[NewGalleryNamePrompt] = None,
    actual_name_out: Optional[List[str]] = None,
    use_folder_name_password: bool = True,
) -> str:
    endpoint = f"https://api.photodeck.com/websites/{URL_NAME}/galleries.xml"
    raw_folder_name = str(folder_name or "").strip()
    password, attempt_name = _derive_gallery_name_and_password(raw_folder_name, use_folder_name_password)
    existing_gallery_prompt = on_existing_gallery or prompt_existing_gallery_action
    new_gallery_name_prompt = on_new_gallery_name or prompt_new_gallery_name

    def remember_actual_name() -> None:
        if actual_name_out is not None:
            actual_name_out[:] = [attempt_name]

    while True:
        existing_uuid, _ = find_existing_folder_any_name(client, parent_uuid, [attempt_name, raw_folder_name])
        if existing_uuid:
            action = existing_gallery_prompt(attempt_name, existing_uuid, None)
            if action == "cancel":
                raise UserCancelled("Upload cancelled by user.")
            if action == "use_existing":
                _sync_existing_gallery_metadata(client, existing_uuid, attempt_name, password)
                remember_actual_name()
                print(f"Using existing gallery '{attempt_name}' ({existing_uuid})")
                return existing_uuid
            new_name = new_gallery_name_prompt(attempt_name, None)
            if new_name is None:
                raise UserCancelled("Upload cancelled by user.")
            attempt_name = new_name
            continue

        data = {
            'gallery[name]': attempt_name,
            'gallery[parent]': parent_uuid,
        }

        data.update(_gallery_fields(attempt_name, password))

        try:
            response = client.api_call('POST', endpoint, data=data)
            break
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 422:
                detail = (exc.response.text or "").strip()
                detail = detail.replace("\r", " ").replace("\n", " ")
                detail_snippet = detail[:500]
                print(f"[WARN] 422 creating gallery '{attempt_name}': {detail_snippet}")

                existing_uuid_retry, _ = find_existing_folder_any_name(client, parent_uuid, [attempt_name, raw_folder_name])
                if existing_uuid_retry:
                    action = existing_gallery_prompt(attempt_name, existing_uuid_retry, detail_snippet)
                    if action == "cancel":
                        raise UserCancelled("Upload cancelled by user.")
                    if action == "use_existing":
                        _sync_existing_gallery_metadata(client, existing_uuid_retry, attempt_name, password)
                        remember_actual_name()
                        print(f"Using existing gallery '{attempt_name}' ({existing_uuid_retry})")
                        return existing_uuid_retry

                new_name = new_gallery_name_prompt(attempt_name, detail_snippet)
                if new_name is None:
                    raise UserCancelled("Upload cancelled by user.")
                attempt_name = new_name
                continue
            raise

    root = ET.fromstring(response.text)
    gallery_id = root.findtext('gallery-uuid')
    remember_actual_name()
    print(f"Copied {attempt_name} successfully")
    if gallery_id is None:
        raise ValueError("PhotoDeck did not return a gallery UUID.")
    return gallery_id


def update_existing_gallery_passwords(
    source_folder: str,
    destination_parent: str,
    client: PhotoDeckClient,
    use_folder_name_password: bool = True,
) -> None:
    if not os.path.exists(source_folder):
        print(f"Source folder '{source_folder}' does not exist.")
        return

    folder_name = os.path.basename(source_folder)
    password, gallery_name = _derive_gallery_name_and_password(folder_name, use_folder_name_password)
    gallery_uuid, _ = find_existing_folder_any_name(client, destination_parent, [gallery_name, folder_name])

    if gallery_uuid is None:
        print(f"Gallery '{gallery_name}' not found under parent {destination_parent}; skipping.")
        return

    fields = _gallery_fields(gallery_name, password)
    if fields:
        client.update_gallery(URL_NAME, gallery_uuid, fields)
        print(f"Updated gallery metadata for '{gallery_name}' ({gallery_uuid})")
    else:
        print(f"No metadata found in folder name '{folder_name}'; leaving gallery '{gallery_name}' unchanged.")

    for item in sorted(os.listdir(source_folder), reverse=True):
        source_path = os.path.join(source_folder, item)
        if os.path.isdir(source_path):
            update_existing_gallery_passwords(
                source_path,
                gallery_uuid,
                client,
                use_folder_name_password=use_folder_name_password,
            )


def copy_folders(
    source_folder: str,
    destination_parent: str,
    client: PhotoDeckClient,
    uploader: BulkUploader,
    on_existing_gallery: Optional[ExistingGalleryPrompt] = None,
    on_new_gallery_name: Optional[NewGalleryNamePrompt] = None,
    cancel_event: Optional[threading.Event] = None,
    use_folder_name_password: bool = True,
    deferred_cover_jobs: Optional[List[Tuple[str, str, Optional[str], Optional[str]]]] = None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UserCancelled("Upload cancelled by user.")
    if not os.path.exists(source_folder):
        print(f"Source folder '{source_folder}' does not exist.")
        return

    destination_uuid = create_folder(
        client,
        os.path.basename(source_folder),
        destination_parent,
        on_existing_gallery=on_existing_gallery,
        on_new_gallery_name=on_new_gallery_name,
        use_folder_name_password=use_folder_name_password,
    )
    _password, gallery_upload_name = _derive_gallery_name_and_password(
        os.path.basename(source_folder),
        use_folder_name_password,
    )

    items = sorted(os.listdir(source_folder), reverse=True)
    cover_items = [item for item in items if os.path.isfile(os.path.join(source_folder, item)) and item.strip().lower().startswith("#cover")]
    regular_items = [item for item in items if item not in cover_items]
    ordered_items = cover_items + regular_items

    for item in ordered_items:
        if cancel_event is not None and cancel_event.is_set():
            raise UserCancelled("Upload cancelled by user.")
        source_path = os.path.join(source_folder, item)
        if os.path.isdir(source_path):
            copy_folders(
                source_path,
                destination_uuid,
                client,
                uploader,
                on_existing_gallery=on_existing_gallery,
                on_new_gallery_name=on_new_gallery_name,
                cancel_event=cancel_event,
                use_folder_name_password=use_folder_name_password,
                deferred_cover_jobs=deferred_cover_jobs,
            )
        else:
            class_name = extract_class_name(os.path.basename(source_folder))
            if item.strip().lower().startswith("#cover"):
                if deferred_cover_jobs is not None:
                    deferred_cover_jobs.append((source_path, destination_uuid, class_name, gallery_upload_name))
                else:
                    uploader.upload_now(source_path, destination_uuid, class_name, gallery_upload_name)
            else:
                uploader.add_job(source_path, destination_uuid, class_name, gallery_upload_name)


def folder_picker() -> Optional[str]:
    if tk is None or filedialog is None:
        raise RuntimeError("tkinter is unavailable for folder picker.")
    root = tk.Tk()
    root.withdraw()
    folder_path = filedialog.askdirectory()
    root.destroy()
    return folder_path or None


def prompt_picture_day_id() -> Optional[str]:
    if tk is None or simpledialog is None:
        raise RuntimeError("tkinter is unavailable for picture day prompt.")
    root = tk.Tk()
    root.withdraw()
    picture_day_id = simpledialog.askstring("Picture Day ID", "Enter the Picture Day ID:", parent=root)
    root.destroy()
    if picture_day_id is None:
        return None
    picture_day_id = picture_day_id.strip()
    return picture_day_id or None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload folders to PhotoDeck galleries.")
    parser.add_argument("--root-folder", dest="root_folder", help="Root folder to upload recursively.")
    parser.add_argument("--max-workers", dest="max_workers", type=int, default=4, help="Number of parallel upload workers.")
    parser.add_argument("--picture-day-id", dest="picture_day_id", help="Prefix appended to uploaded file names.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    root_folder = args.root_folder or folder_picker()
    if not root_folder:
        print("No folder selected.")
        return

    picture_day_id = args.picture_day_id or prompt_picture_day_id()
    if not picture_day_id:
        print("No Picture Day ID entered.")
        return

    if not all([API_KEY, API_SECRET, EMAIL_ADDRESS, PASSWORD]):
        raise RuntimeError("API credentials are missing. Ensure API_KEY, API_SECRET, EMAIL_ADDRESS, and PASSWORD are set.")

    client = PhotoDeckClient(API_KEY, API_SECRET, EMAIL_ADDRESS, PASSWORD)

    uploader = BulkUploader(client, max_workers=args.max_workers, filename_prefix=picture_day_id)

    try:
        copy_folders(root_folder, DEFAULT_PARENT_GALLERY, client, uploader)
    except UserCancelled as user_exc:
        print(str(user_exc))
    finally:
        uploader.close()


if __name__ == "__main__":
    main()
