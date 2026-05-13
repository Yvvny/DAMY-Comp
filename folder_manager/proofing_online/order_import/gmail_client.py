from __future__ import annotations

import base64
import html as html_lib
import mimetypes
import os
import re
import time
from pathlib import Path
from email.message import EmailMessage
from typing import Dict, Iterable, List, Optional, Sequence, Set

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import SCOPES
from .parsers import validate_picture_day_id
from .utils import emit_status
from folder_manager.config import ORDER_IMPORT_CREDENTIALS_PATH, ORDER_IMPORT_TOKEN_PATH


def _dedupe_paths(paths: Sequence[Path]) -> List[Path]:
    seen: set[str] = set()
    result: List[Path] = []
    for path in paths:
        normalized = str(path.expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(path)
    return result


def _token_candidates() -> List[Path]:
    explicit = os.getenv("DAMY_ORDER_IMPORT_TOKEN_PATH")
    candidates = [
        Path(explicit).expanduser() if explicit else Path(ORDER_IMPORT_TOKEN_PATH).expanduser(),
        Path(ORDER_IMPORT_TOKEN_PATH).expanduser(),
        Path.cwd() / "token.json",
        Path(__file__).resolve().with_name("token.json"),
    ]
    return _dedupe_paths(candidates)


def _credentials_candidates() -> List[Path]:
    explicit = os.getenv("DAMY_ORDER_IMPORT_CREDENTIALS_PATH")
    repo_root = Path(__file__).resolve().parents[3]
    candidates = [
        Path(explicit).expanduser() if explicit else Path(ORDER_IMPORT_CREDENTIALS_PATH).expanduser(),
        Path(ORDER_IMPORT_CREDENTIALS_PATH).expanduser(),
        repo_root / "folder_manager" / "calendar_import_v3" / "credentials.json",
        repo_root / "folder_manager" / "order_import_v1" / "credentials.json",
        repo_root / "calendar_import_v3" / "credentials.json",
        repo_root / "order_import_v1" / "credentials.json",
        Path.cwd() / "credentials.json",
        Path(__file__).resolve().with_name("credentials.json"),
    ]
    return _dedupe_paths(candidates)


def _first_existing(paths: Sequence[Path]) -> Optional[Path]:
    for path in paths:
        try:
            if path.is_file():
                return path
        except Exception:
            continue
    return None


def ensure_credentials() -> Credentials:
    creds = None
    token_candidates = _token_candidates()
    token_path = _first_existing(token_candidates) or token_candidates[0]

    if token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            try:
                token_path.unlink()
            except OSError:
                pass
            creds = None

    required_scopes = {scope.lower() for scope in SCOPES}
    if creds:
        current_scopes = {scope.lower() for scope in (creds.scopes or [])}
        if not required_scopes.issubset(current_scopes):
            try:
                token_path.unlink()
            except OSError:
                pass
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                try:
                    token_path.unlink()
                except OSError:
                    pass
                creds = None
        if not creds or not creds.valid:
            credentials_candidates = _credentials_candidates()
            credentials_path = _first_existing(credentials_candidates)
            if credentials_path is None:
                searched = "\n".join(f"- {p}" for p in credentials_candidates)
                raise FileNotFoundError(
                    "No credentials.json file found for Gmail OAuth.\n"
                    f"Searched:\n{searched}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'w', encoding='utf-8') as token_file:
            token_file.write(creds.to_json())

    return creds


def get_gmail_service():
    creds = ensure_credentials()
    return build('gmail', 'v1', credentials=creds)


def _is_retryable_gmail_error(error: HttpError) -> bool:
    status = int(getattr(getattr(error, 'resp', None), 'status', 0) or 0)
    text = str(error).lower()
    if status in {429, 500, 502, 503, 504}:
        return True
    if status == 403 and ('ratelimit' in text or 'user-rate' in text or 'quota' in text):
        return True
    return False


def _fetch_full_message_with_retry(service, message_id: str, max_attempts: int = 5) -> Optional[dict]:
    for attempt in range(max_attempts):
        try:
            return service.users().messages().get(
                userId='me',
                id=message_id,
                format='full',
            ).execute()
        except HttpError as error:
            if attempt >= max_attempts - 1 or not _is_retryable_gmail_error(error):
                emit_status(f'Gmail API error while fetching message {message_id}: {error}')
                return None
            delay = min(2 ** attempt, 16)
            emit_status(
                f'Gmail API rate limit while fetching message {message_id}; retrying in {delay}s...',
            )
            time.sleep(delay)
    return None


def fetch_messages_with_metadata(
    service,
    query: str,
    max_results: int,
    label_ids: Optional[Sequence[str]] = None,
) -> List[dict]:
    messages: List[dict] = []
    try:
        request = service.users().messages().list(
            userId='me',
            q=query,
            labelIds=list(label_ids) if label_ids else None,
            maxResults=min(max_results, 500),
            includeSpamTrash=False,
        )

        while request is not None and len(messages) < max_results:
            response = request.execute()
            message_refs = response.get('messages', [])

            if not message_refs:
                break

            remaining = max_results - len(messages)
            batch_refs = message_refs[:remaining]

            if batch_refs:
                for ref in batch_refs:
                    msg_id = ref.get('id')
                    if not msg_id:
                        continue
                    message = _fetch_full_message_with_retry(service, msg_id)
                    if not message:
                        continue
                    label_ids = set(message.get('labelIds') or [])
                    if 'TRASH' in label_ids:
                        continue
                    messages.append(message)
                    time.sleep(0.05)

            if len(messages) >= max_results:
                break

            request = service.users().messages().list_next(
                previous_request=request,
                previous_response=response,
            )
    except HttpError as error:
        emit_status(f'Gmail API error during fetch: {error}')

    return messages


def get_header_value(message: dict, header_name: str) -> Optional[str]:
    headers = message.get('payload', {}).get('headers', [])
    for header in headers:
        if header['name'].lower() == header_name.lower():
            return header['value']
    return None


def get_html_from_payload(payload: dict) -> Optional[str]:
    if payload.get('mimeType') == 'text/html':
        data = payload.get('body', {}).get('data')
        if data:
            decoded = base64.urlsafe_b64decode(data)
            return decoded.decode('utf-8', errors='replace')

    for part in payload.get('parts', []):
        html = get_html_from_payload(part)
        if html:
            return html

    return None


def message_contains_picture_day_id(message: dict, picture_day_id: str) -> bool:
    upper_id = picture_day_id.upper()

    subject = get_header_value(message, 'Subject') or ''
    if upper_id in subject.upper():
        return True

    html_content = get_html_from_payload(message.get('payload', {}))
    if html_content and upper_id in html_content.upper():
        return True

    return False


def get_plain_text_from_payload(payload: dict) -> Optional[str]:
    if payload.get('mimeType') == 'text/plain':
        data = payload.get('body', {}).get('data')
        if data:
            decoded = base64.urlsafe_b64decode(data)
            return decoded.decode('utf-8', errors='replace')

    for part in payload.get('parts', []):
        text = get_plain_text_from_payload(part)
        if text:
            return text

    return None


_PICTURE_DAY_ID_PATTERN = re.compile(r'\b([PH]\d{7,8})\b', re.IGNORECASE)
_PICTURE_DAY_ID_FUZZY_PATTERN = re.compile(r'([PH]\d{7,8})', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _extract_ids_from_text(text: Optional[str]) -> Set[str]:
    if not text:
        return set()
    normalized_matches: Set[str] = set()

    for pattern in (_PICTURE_DAY_ID_PATTERN, _PICTURE_DAY_ID_FUZZY_PATTERN):
        found = pattern.findall(text)
        if not found:
            continue
        for candidate in found:
            upper = candidate.upper()
            if validate_picture_day_id(upper):
                normalized_matches.add(upper)
        if normalized_matches:
            break

    return normalized_matches


def extract_picture_day_ids(message: dict) -> List[str]:
    payload = message.get('payload', {}) or {}
    subject = get_header_value(message, 'Subject') or ''
    html_content = get_html_from_payload(payload) or ''
    plain_text = get_plain_text_from_payload(payload) or ''

    html_unescaped = html_lib.unescape(html_content) if html_content else ''
    html_stripped = _HTML_TAG_RE.sub(' ', html_unescaped) if html_unescaped else ''

    candidates: Set[str] = set()
    for text in (subject, html_content, html_unescaped, html_stripped, plain_text):
        candidates.update(_extract_ids_from_text(text))

    return sorted(candidates)


def get_message_debug_preview(message: dict, max_length: int = 600) -> Dict[str, str]:
    payload = message.get('payload', {}) or {}
    subject = get_header_value(message, 'Subject') or ''
    snippet = message.get('snippet') or ''
    html_content = get_html_from_payload(payload) or ''
    plain_text = get_plain_text_from_payload(payload) or ''

    html_unescaped = html_lib.unescape(html_content) if html_content else ''
    html_stripped = _HTML_TAG_RE.sub(' ', html_unescaped) if html_unescaped else ''

    def _trim(value: str) -> str:
        cleaned = re.sub(r'\s+', ' ', value).strip()
        if len(cleaned) <= max_length:
            return cleaned
        return cleaned[: max_length - 3] + '...'

    return {
        'subject': subject,
        'snippet': _trim(snippet),
        'html_raw': _trim(html_content),
        'html_unescaped': _trim(html_unescaped),
        'html_text': _trim(html_stripped),
        'plain_text': _trim(plain_text),
    }


def fetch_messages_by_label(service, label_id: str, max_results: int) -> List[dict]:
    return fetch_messages_with_metadata(service, '', max_results, label_ids=[label_id])


def list_label_ids_by_name(service) -> Dict[str, str]:
    try:
        response = service.users().labels().list(userId='me').execute()
    except HttpError as error:
        emit_status(f'Gmail API error while listing labels: {error}')
        return {}
    labels = response.get('labels', []) or []
    return {label.get('name', ''): label.get('id', '') for label in labels if label.get('name') and label.get('id')}


def get_label_id_by_name(service, label_name: str) -> Optional[str]:
    label_map = list_label_ids_by_name(service)
    return label_map.get(label_name)


def ensure_label_exists(service, label_name: str) -> Optional[str]:
    label_id = get_label_id_by_name(service, label_name)
    if label_id:
        return label_id

    body = {
        'name': label_name,
        'labelListVisibility': 'labelShow',
        'messageListVisibility': 'show',
    }
    try:
        response = service.users().labels().create(userId='me', body=body).execute()
    except HttpError as error:
        emit_status(f'Gmail API error while creating label "{label_name}": {error}')
        return None
    return response.get('id')


def modify_message_labels(
    service,
    message_id: str,
    add_label_ids: Optional[Iterable[str]] = None,
    remove_label_ids: Optional[Iterable[str]] = None,
) -> None:
    body: Dict[str, List[str]] = {}
    if add_label_ids:
        body['addLabelIds'] = list(add_label_ids)
    if remove_label_ids:
        body['removeLabelIds'] = list(remove_label_ids)
    if not body:
        return
    try:
        service.users().messages().modify(userId='me', id=message_id, body=body).execute()
    except HttpError as error:
        emit_status(f'Gmail API error while modifying labels for message {message_id}: {error}')


def send_email_with_attachments(
    service,
    *,
    to: str,
    subject: str,
    body_text: str,
    attachment_paths: Optional[Sequence[str]] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> dict:
    message = EmailMessage()
    message["To"] = (to or "").strip()
    message["Subject"] = (subject or "").strip()
    if cc and str(cc).strip():
        message["Cc"] = str(cc).strip()
    if bcc and str(bcc).strip():
        message["Bcc"] = str(bcc).strip()
    message.set_content((body_text or "").strip() or " ")

    for path in list(attachment_paths or []):
        file_path = str(path or "").strip()
        if not file_path:
            continue
        with open(file_path, "rb") as handle:
            data = handle.read()
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=os.path.basename(file_path),
        )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        return (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
    except HttpError as error:
        emit_status(f"Gmail API error while sending email: {error}")
        raise


def send_email_with_inline_assets(
    service,
    *,
    to: str,
    subject: str,
    body_text: str,
    html_body: str,
    inline_image_path: Optional[str] = None,
    inline_image_cid: str = "proof_preview",
    attachment_paths: Optional[Sequence[str]] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> dict:
    message = EmailMessage()
    message["To"] = (to or "").strip()
    message["Subject"] = (subject or "").strip()
    if cc and str(cc).strip():
        message["Cc"] = str(cc).strip()
    if bcc and str(bcc).strip():
        message["Bcc"] = str(bcc).strip()

    message.set_content((body_text or "").strip() or " ")
    message.add_alternative((html_body or "").strip() or "<p></p>", subtype="html")

    if inline_image_path and str(inline_image_path).strip():
        file_path = str(inline_image_path).strip()
        with open(file_path, "rb") as handle:
            data = handle.read()
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "image", "jpeg"
        html_part = message.get_payload()[-1]
        html_part.add_related(
            data,
            maintype=maintype,
            subtype=subtype,
            cid=f"<{inline_image_cid}>",
            filename=os.path.basename(file_path),
        )

    for path in list(attachment_paths or []):
        file_path = str(path or "").strip()
        if not file_path:
            continue
        with open(file_path, "rb") as handle:
            data = handle.read()
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=os.path.basename(file_path),
        )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        return (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
    except HttpError as error:
        emit_status(f"Gmail API error while sending email: {error}")
        raise
