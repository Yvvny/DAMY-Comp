from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from email.utils import parseaddr
from functools import lru_cache
from typing import Callable, List, Optional

from .config import MAX_MATCHES_PER_ID, MAX_RESULTS_PER_QUERY, ORDER_SOURCES
from .exceptions import NoOrdersFoundError
from .file_manager import (
    create_photo_order_folders,
    delete_files,
    get_pdf_metadata,
    resolve_output_folder,
)
from .gmail_client import (
    fetch_messages_with_metadata,
    get_header_value,
    get_html_from_payload,
    message_contains_picture_day_id,
)
from .parsers import (
    extract_class_number,
    extract_order_number,
    extract_photo_identifiers,
    sanitize_filename,
)
from .pdf_utils import combine_pdfs, get_pdfkit_config
from .search import build_search_queries
from .utils import emit_status

_PACKAGE_PREFIX_RE = re.compile(r'(?i)^package\s+')
_NON_ALNUM_ANY_RE = re.compile(r'[^A-Za-z0-9]+')
_LETTER_RE = re.compile(r'[A-Z]')
_DIGITS_RE = re.compile(r'(\d+)')
_CLASS_SORT_DIGIT_RE = re.compile(r'^\s*(?:class\s*)?(\d+)', re.IGNORECASE)
_WHITESPACE_COLLAPSE_RE = re.compile(r'\s+')
_IMAGE_EXTENSION_RE = re.compile(r'\.(?:jpe?g|png|tiff?|bmp|gif|webp)$', re.IGNORECASE)
_TRAILING_SMALLTHUMB_RE = re.compile(r'_smallthumb$', re.IGNORECASE)


def process_picture_day(
    service,
    picture_day_id: str,
    order_type: str,
    *,
    base_folder_override: Optional[str] = None,
    created_folders_out: Optional[List[str]] = None,
    processed_message_ids_out: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    source_settings = ORDER_SOURCES[order_type]
    from_address = source_settings['from_address']
    base_directory = source_settings['base_directory']
    use_broad_first = source_settings.get('use_broad_search_first', False)
    if order_type == 'photodeck' and base_folder_override:
        use_broad_first = False

    search_queries = build_search_queries(
        picture_day_id,
        from_address,
        use_broad_first,
        source_settings.get('gmail_label'),
    )

    all_messages: List[dict] = []

    for query, description in search_queries:
        emit_status(
            f"{picture_day_id}: Searching with {description}: [{query}]",
            progress_callback,
        )

        messages = fetch_messages_with_metadata(service, query, MAX_RESULTS_PER_QUERY)
        emit_status(
            f"{picture_day_id}: Found {len(messages)} message(s)",
            progress_callback,
        )

        if messages:
            all_messages = messages
            break

    if not all_messages:
        raise NoOrdersFoundError(
            f"No {source_settings['display_name']} orders found for Picture Day ID {picture_day_id} "
            f"after trying {len(search_queries)} search strategies."
        )

    email_entries: List[dict] = []
    seen_entries = set()

    for message in all_messages:
        msg_id = message.get('id')

        from_header = get_header_value(message, 'From') or ''
        _, actual_address = parseaddr(from_header)
        if actual_address.lower() != from_address.lower():
            continue

        if not message_contains_picture_day_id(message, picture_day_id):
            continue

        html_content = get_html_from_payload(message.get('payload', {}))
        if not html_content:
            continue

        subject = get_header_value(message, 'Subject') or 'Order'
        class_number = extract_class_number(html_content)

        content_hash = hashlib.sha1(html_content.encode('utf-8')).hexdigest()
        key = (subject, class_number, content_hash)

        if key in seen_entries:
            continue

        seen_entries.add(key)

        emit_status(
            f"{picture_day_id}: Queued message {msg_id} - Subject '{subject}', Class '{class_number}'",
            progress_callback,
        )

        email_entries.append(
            {
                'subject': subject,
                'content': html_content,
                'class_number': class_number,
                'order_number': extract_order_number(html_content),
                'message_id': msg_id,
            }
        )

        if len(email_entries) >= MAX_MATCHES_PER_ID:
            emit_status(
                f"{picture_day_id}: Reached match limit ({MAX_MATCHES_PER_ID})",
                progress_callback,
            )
            break

    if not email_entries:
        raise NoOrdersFoundError(
            f"No valid {source_settings['display_name']} orders found for Picture Day ID {picture_day_id}."
        )

    if order_type == 'photodeck':
        for entry in email_entries:
            identifiers = extract_photo_identifiers(entry['content'], picture_day_id)
            order_number = entry.get('order_number')
            order_number = order_number.strip().upper() if isinstance(order_number, str) else None
            for identifier in identifiers:
                if order_number:
                    identifier['order_number'] = order_number
                _prepare_identifier_metadata(
                    identifier,
                    entry.get('class_number'),
                    entry.get('subject'),
                )
            if identifiers:
                class_override = _class_descriptor_from_identifiers(identifiers)
                if class_override and _is_missing_class(entry.get('class_number')):
                    entry['class_number'] = class_override
            entry['identifiers'] = identifiers
        email_entries.sort(key=_photodeck_entry_sort_key)
    else:
        email_entries.sort(key=lambda item: _class_sort_key(item.get('class_number')))

    pdfkit_config = get_pdfkit_config()
    render_jobs: List[tuple] = []

    for index, entry in enumerate(email_entries, start=1):
        subject = sanitize_filename(entry['subject'])
        class_part = sanitize_filename(entry['class_number']) if entry['class_number'] else 'ZZZ'
        base_name = f"{subject} - {class_part}" if class_part.lower() != subject.lower() else subject
        pdf_filename = f"{index:02d} {base_name}.pdf"
        emit_status(f"{picture_day_id}: Rendering PDF for '{base_name}'", progress_callback)
        render_jobs.append((index - 1, pdf_filename, entry['content']))

    pdf_files: List[Optional[str]] = [None] * len(render_jobs)

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(_render_pdf, filename, content, pdfkit_config): (slot, filename)
            for slot, filename, content in render_jobs
        }
        for future in as_completed(future_map):
            slot, filename = future_map[future]
            future.result()
            pdf_files[slot] = filename

    pdf_files = [filename for filename in pdf_files if filename]
    for idx, path in enumerate(pdf_files):
        abs_path = os.path.abspath(path)
        pdf_files[idx] = abs_path
        if idx < len(email_entries):
            email_entries[idx]['pdf_path'] = abs_path

    today = date.today().strftime('%y%m%d')
    if order_type == 'photodeck' and base_folder_override:
        folder_path = os.path.abspath(base_folder_override)
        if not os.path.isdir(folder_path):
            raise FileNotFoundError(f"PhotoDeck import folder does not exist: {folder_path}")
        emit_status(
            f"{picture_day_id}: Using existing Stage 5 folder {folder_path}",
            progress_callback,
        )
    else:
        folder_path = resolve_output_folder(base_directory, picture_day_id, today)
    combined_pdf_path = folder_path

    if order_type != 'photodeck':
        combined_pdf_filename = f"{today} {picture_day_id.upper()} Ordered {len(pdf_files)}.pdf"
        emit_status(
            f"{picture_day_id}: Combining {len(pdf_files)} PDF(s) into '{combined_pdf_filename}'",
            progress_callback,
        )

        combined_pdf_path = os.path.join(folder_path, combined_pdf_filename)
        combine_pdfs(pdf_files, combined_pdf_path)
        emit_status(f"{picture_day_id}: Combined PDF saved to {combined_pdf_path}", progress_callback)
    else:
        emit_status(
            f"{picture_day_id}: Skipping combined PDF (use Combine Selected PDFs when ready)",
            progress_callback,
        )

    if order_type == 'photodeck':
        for entry in email_entries:
            pdf_path = entry.get('pdf_path')
            if not pdf_path:
                continue
            for identifier in entry.get('identifiers', []):
                identifier['__pdf_path'] = pdf_path

        emit_status(f"{picture_day_id}: Creating individual photo order folders...", progress_callback)

        all_identifiers: List[dict] = []
        for entry in email_entries:
            identifiers = entry.get('identifiers', [])
            all_identifiers.extend(identifiers)

        unique_identifiers: List[dict] = []
        seen_signatures = set()
        for identifier in all_identifiers:
            signature = _identifier_signature(identifier)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            unique_identifiers.append(identifier)

        emit_status(
            f"{picture_day_id}: Found {len(unique_identifiers)} unique photo(s) to organize",
            progress_callback,
        )

        if unique_identifiers:
            created_folders = create_photo_order_folders(
                folder_path,
                unique_identifiers,
                picture_day_id,
                progress_callback,
            )
            if created_folders_out is not None:
                created_folders_out.extend(created_folders)
        else:
            emit_status(
                f"{picture_day_id}: No photo identifiers found in orders",
                progress_callback,
            )

    if processed_message_ids_out is not None:
        for entry in email_entries:
            msg_id = str(entry.get('message_id') or '').strip()
            if msg_id and msg_id not in processed_message_ids_out:
                processed_message_ids_out.append(msg_id)

    delete_files(pdf_files)
    emit_status(f"{picture_day_id}: Temporary PDFs deleted", progress_callback)

    return combined_pdf_path


def _identifier_signature(identifier: dict) -> tuple:
    original_id = _normalize_identifier_id(identifier.get('original_id'))
    proof_id = _normalize_identifier_id(identifier.get('proof_id'))
    detail_signature = identifier.get('__detail_signature')
    if detail_signature is None:
        detail_signature = _compute_detail_signature(identifier)
        identifier['__detail_signature'] = detail_signature
    return (original_id, proof_id, *detail_signature)


def _normalize_identifier_id(value: Optional[str]) -> str:
    cleaned = _normalize_detail_value(value)
    if not cleaned:
        return ''
    cleaned = _IMAGE_EXTENSION_RE.sub('', cleaned).rstrip('_')
    return _TRAILING_SMALLTHUMB_RE.sub('', cleaned).rstrip('_')


@lru_cache(maxsize=1024)
def _normalize_detail_value(value: Optional[str]) -> str:
    if value is None:
        return ''
    value_str = str(value)
    if not value_str:
        return ''
    return _WHITESPACE_COLLAPSE_RE.sub(' ', value_str).strip().lower()


def _prepare_identifier_metadata(
    identifier: dict,
    class_number: Optional[str],
    subject: Optional[str],
) -> None:
    detail_signature = _compute_detail_signature(identifier)
    identifier['__detail_signature'] = detail_signature

    details = identifier.get('details') or {}
    package_code = _normalize_package_code(details.get('package'))
    if package_code:
        identifier['__package_sort_key'] = _package_sort_key(package_code)
    else:
        identifier['__package_sort_key'] = _PACKAGE_SORT_FALLBACK

    proof_id = identifier.get('proof_id') or identifier.get('original_id') or ''
    identifier['__sequence_sort_key'] = (
        _extract_sequence_number(proof_id),
        proof_id,
    )
    identifier['__class_sort_key'] = _class_sort_key(class_number)
    identifier['__class_number'] = class_number or ''
    identifier['__source_subject'] = subject or ''


def _compute_detail_signature(identifier: dict) -> tuple:
    details = identifier.get('details') or {}
    package = _normalize_detail_value(details.get('package'))
    background = _normalize_detail_value(details.get('background'))
    addons_raw = details.get('addons') or []

    normalized_addons: List[str] = []
    seen = set()
    for entry in addons_raw:
        if isinstance(entry, tuple):
            value = entry[1]
        elif isinstance(entry, dict):
            value = entry.get('value')
        else:
            value = entry
        normalized = _normalize_detail_value(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_addons.append(normalized)

    normalized_addons.sort()
    quantity = _normalize_detail_value(details.get('quantity'))
    return (package, tuple(normalized_addons), background, quantity)


def _photodeck_entry_sort_key(entry: dict) -> tuple:
    identifiers = entry.get('identifiers') or []
    package_key = _package_priority_from_identifiers(identifiers)
    sequence_key = _sequence_sort_value(identifiers)
    class_key = _class_sort_key(entry.get('class_number'))
    subject = entry.get('subject') or ''
    return (class_key, package_key, sequence_key, subject)


_PACKAGE_SORT_FALLBACK = (999, 0, '')
_SEQUENCE_SORT_FALLBACK = (10**12, '')
_CLASS_SORT_FALLBACK = (2, 0, '')


def _class_descriptor_from_identifiers(identifiers: List[dict]) -> Optional[str]:
    for identifier in identifiers:
        descriptor = identifier.get('class_descriptor')
        if descriptor:
            cleaned = str(descriptor).strip()
            if cleaned:
                return cleaned
    return None


def _is_missing_class(value: Optional[str]) -> bool:
    if not value:
        return True
    normalized = value.strip()
    if not normalized:
        return True
    return normalized.upper() in {'ZZZ', 'UNKNOWN'}


def _class_sort_key(class_value: Optional[str]) -> tuple:
    if not class_value:
        return _CLASS_SORT_FALLBACK
    text = str(class_value).strip()
    if not text or _is_missing_class(text):
        return _CLASS_SORT_FALLBACK

    digit_match = _CLASS_SORT_DIGIT_RE.match(text)
    if digit_match:
        number = int(digit_match.group(1))
        suffix = text[digit_match.end() :].strip().lower()
        return (0, number, suffix)

    normalized = text.lower()
    return (1, 0, normalized)


def _package_priority_from_identifiers(identifiers: List[dict]) -> tuple:
    keys = [
        identifier.get('__package_sort_key')
        for identifier in identifiers
        if identifier.get('__package_sort_key') is not None
    ]
    if keys:
        return min(keys)
    return _PACKAGE_SORT_FALLBACK


@lru_cache(maxsize=1024)
def _normalize_package_code(value: Optional[str]) -> str:
    if value is None:
        return ''
    cleaned = _PACKAGE_PREFIX_RE.sub('', str(value)).strip()
    cleaned = _NON_ALNUM_ANY_RE.sub('', cleaned)
    if not cleaned:
        return ''
    cleaned = cleaned.upper()
    if cleaned.startswith('P') and len(cleaned) > 1:
        return cleaned
    if len(cleaned) == 1 and cleaned.isalpha():
        return f'P{cleaned}'
    return f'P{cleaned}'


def _package_sort_key(code: str) -> tuple:
    normalized = code.upper()
    letter = ''
    remainder = ''
    if normalized.startswith('P') and len(normalized) > 1 and normalized[1].isalpha():
        letter = normalized[1]
        remainder = normalized[2:]
    else:
        match = _LETTER_RE.search(normalized)
        if match:
            letter = match.group(0)
            remainder = normalized[match.start() + 1 :]
        else:
            remainder = normalized
    letter_priority = ord(letter) - ord('A') if letter else 999
    number_match = _DIGITS_RE.search(remainder)
    number_value = int(number_match.group(1)) if number_match else 0
    return (letter_priority, number_value, normalized)


def _sequence_sort_value(identifiers: List[dict]) -> tuple:
    values = [
        identifier.get('__sequence_sort_key')
        for identifier in identifiers
        if identifier.get('__sequence_sort_key') is not None
    ]
    if values:
        return min(values)
    return _SEQUENCE_SORT_FALLBACK


def _extract_sequence_number(proof_id: str) -> int:
    if not proof_id:
        return 10**12
    cleaned = proof_id.strip('_')
    tokens = [token for token in cleaned.split('_') if token]
    if not tokens:
        return 10**12

    variant_index = len(tokens)
    for idx, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in {'v', 'h'} and idx >= 3:
            variant_index = idx
            break

    search_tokens = tokens[:variant_index]
    if len(search_tokens) < 3 and len(tokens) >= 3:
        search_tokens = tokens[:3]

    digit_candidates = [
        (idx, token, int(token))
        for idx, token in enumerate(search_tokens)
        if token.isdigit()
    ]

    prioritized = [entry for entry in digit_candidates if len(entry[1]) >= 4]
    if prioritized:
        prioritized.sort(key=lambda item: (item[0], item[2]))
        return prioritized[0][2]

    prioritized = [entry for entry in digit_candidates if entry[0] >= 2]
    if prioritized:
        prioritized.sort(key=lambda item: (item[0], item[2]))
        return prioritized[0][2]

    if digit_candidates:
        digit_candidates.sort(key=lambda item: (item[0], item[2]))
        return digit_candidates[0][2]

    match = _DIGITS_RE.search(proof_id)
    if match:
        return int(match.group(1))
    return 10**12


def _render_pdf(filename: str, html_content: str, pdfkit_config) -> None:
    try:
        import pdfkit  # type: ignore
    except Exception as exc:
        raise ModuleNotFoundError(
            "pdfkit is required for PDF rendering. Install pdfkit to enable order PDF generation."
        ) from exc
    pdfkit.from_string(html_content, filename, configuration=pdfkit_config)


def combine_selected_order_pdfs(pdf_paths: List[str], output_path: str) -> List[str]:
    if not pdf_paths:
        raise ValueError('No PDF files selected.')

    resolved_paths = [os.path.abspath(path) for path in pdf_paths if os.path.isfile(path)]
    if not resolved_paths:
        raise FileNotFoundError('None of the selected PDF files exist.')

    sortable_entries: List[tuple] = []

    for pdf_path in resolved_paths:
        filename = os.path.basename(pdf_path)
        metadata = get_pdf_metadata(pdf_path)

        class_value = metadata.get('class_descriptor')
        class_key = _class_sort_key(class_value)

        package_key_raw = metadata.get('package_sort_key')
        if isinstance(package_key_raw, list):
            package_key = tuple(package_key_raw)
        elif isinstance(package_key_raw, tuple):
            package_key = package_key_raw
        elif package_key_raw is not None:
            package_key = (0, 0, str(package_key_raw))
        else:
            package_key = _PACKAGE_SORT_FALLBACK

        sequence_key_raw = metadata.get('sequence_sort_key')
        if isinstance(sequence_key_raw, list):
            sequence_key = tuple(sequence_key_raw)
        elif isinstance(sequence_key_raw, tuple):
            sequence_key = sequence_key_raw
        elif sequence_key_raw is not None:
            sequence_key = (sequence_key_raw, '')
        else:
            base_id = os.path.splitext(filename)[0]
            sequence_key = (
                _extract_sequence_number(base_id),
                base_id.lower(),
            )

        sortable_entries.append(
            (
                class_key,
                package_key,
                sequence_key,
                filename.lower(),
                pdf_path,
            )
        )

    sortable_entries.sort(key=lambda item: item[:4])
    ordered_paths = [entry[4] for entry in sortable_entries]
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
    combine_pdfs(ordered_paths, output_path)
    return ordered_paths


__all__ = ['process_picture_day', 'combine_selected_order_pdfs']
