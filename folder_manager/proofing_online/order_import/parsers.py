from __future__ import annotations

import html
import re
from datetime import datetime
from functools import lru_cache
from typing import Dict, Iterator, List, Optional, Tuple

_DETAIL_PAIR_PATTERN = re.compile(r'([A-Za-z][A-Za-z0-9\s\-/&]+)\s*[:_]\s*([^,]+)')
_SMALLTHUMB_RE = re.compile(r'_smallthumb(?=\.[^.]+$)', re.IGNORECASE)
_CLASS_NUMBER_RE = re.compile(r'Class Number:\s*([^\r\n<]+)')
_VARIANT_CLASS_RE = re.compile(r'_([0-9]+)[^\s<>]*')
_DATE_CLASS_RE = re.compile(r'P\d{8}_(\d{2})_')
_FALLBACK_CLASS_RE = re.compile(r'P\d{8}_[^_\s]+_([^_\s]+)')
_SANITIZE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE_RE = re.compile(r'\s+')
_ORDER_NUMBER_RE = re.compile(r'\bO-\d{5,}\b', re.IGNORECASE)
_TRAILING_WS_RE = re.compile(r'[.\s]+$')
_SCRIPT_STYLE_RE = re.compile(r'(?is)<(script|style)[^>]*>.*?</\1>')
_BR_TAG_RE = re.compile(r'(?i)<br\s*/?>')
_BLOCK_CLOSE_RE = re.compile(r'(?i)</(p|div|li|tr|td|th|table|tbody|thead|h[1-6])>')
_TAG_RE = re.compile(r'<[^>]+>')
_IMAGE_EXTENSION_RE = re.compile(r'\.(?:jpe?g|png|tiff?|bmp|gif|webp)$', re.IGNORECASE)
_TRAILING_SMALLTHUMB_RE = re.compile(r'_smallthumb$', re.IGNORECASE)
_SEQUENCE_NUM_RE = re.compile(r'^\d{4,}$')
_ID_DATE_CODE_RE = re.compile(r'\d{4}[A-Z]')


def extract_photo_identifiers(html_content: str, picture_day_id: str) -> List[Dict[str, str]]:
    """
    Extract photo identifiers and related metadata from email content.
    """
    identifiers: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, tuple]] = set()

    details_map = _build_order_details_map(html_content, picture_day_id)
    pattern = rf'{re.escape(picture_day_id)}_([^"\s<>]+)'

    for match in re.finditer(pattern, html_content, re.IGNORECASE):
        full_suffix = match.group(1)

        identifier_parts = _parse_identifier_suffix(full_suffix)
        if not identifier_parts:
            continue

        original_id = identifier_parts['core_id']
        proof_id = identifier_parts['proof_suffix']
        detail_base_name = identifier_parts['base_id']
        class_descriptor = identifier_parts.get('class_descriptor') or ''

        detail_entries = details_map.get(original_id) or [{}]

        for detail in detail_entries:
            detail_signature = _detail_signature(detail)
            signature = (original_id.lower(), proof_id.lower(), detail_signature)
            if signature in seen:
                continue
            seen.add(signature)
            identifiers.append(
                {
                    'original_id': original_id,
                    'proof_id': proof_id,
                    'folder_name': f'{original_id} Order',
                    'details': detail or {},
                    'detail_base_name': original_id,
                    'class_descriptor': class_descriptor,
                }
            )

    return identifiers


def _build_order_details_map(html_content: str, picture_day_id: str) -> Dict[str, List[Dict[str, object]]]:
    lines = _clean_text_lines(html_content)
    details_map: Dict[str, List[Dict[str, object]]] = {}

    current_package = ''
    current_background = ''
    current_addons: List[Tuple[str, str]] = []
    current_quantity = 1
    pid_lower = picture_day_id.lower()

    for line_idx, line in enumerate(lines):
        lower_line = line.lower()

        for label, value in _iter_detail_pairs(line):
            label_lower = label.lower()
            if pid_lower == label_lower or pid_lower in label_lower:
                continue
            cleaned_value = _clean_detail_value(value)
            if not cleaned_value:
                continue

            if 'background' in label_lower:
                current_background = cleaned_value
            elif 'package' in label_lower:
                current_package = cleaned_value
                current_addons = []
                current_quantity = 1
            elif 'quantity' in label_lower or label_lower.startswith('qty'):
                qty_value = _parse_quantity_value(cleaned_value)
                if qty_value is not None:
                    current_quantity = qty_value
            else:
                addon_value = _format_addon_value(label, cleaned_value)
                if addon_value and addon_value not in current_addons:
                    current_addons.append(addon_value)

        if 'qty' in lower_line or 'quantity' in lower_line:
            qty_value = _extract_quantity_from_text(line)
            if qty_value is not None:
                current_quantity = qty_value

        if pid_lower not in lower_line:
            continue

        for match in re.finditer(
            rf'{re.escape(picture_day_id)}_([^\s,]+)',
            line,
            re.IGNORECASE,
        ):
            suffix = match.group(1).rstrip('_')
            identifier_parts = _parse_identifier_suffix(suffix)
            if not identifier_parts:
                continue
            original_id = identifier_parts['core_id']
            if not original_id:
                continue

            if current_quantity == 1 and line_idx + 1 < len(lines):
                for lookahead_idx in range(line_idx + 1, min(line_idx + 4, len(lines))):
                    next_line = lines[lookahead_idx]

                    qty_match = re.match(r'^\s*(\d+)\s+[\d.,]+', next_line)
                    if qty_match:
                        current_quantity = max(1, int(qty_match.group(1)))
                        break

                    qty_match = re.match(r'^\s*(\d+)\s*$', next_line)
                    if qty_match and lookahead_idx + 1 < len(lines):
                        next_next_line = lines[lookahead_idx + 1]
                        if re.match(r'^\s*[\d.,]+\s*$', next_next_line):
                            current_quantity = max(1, int(qty_match.group(1)))
                            break

            details_entry = {
                'package': current_package,
                'addons': list(current_addons),
                'background': current_background,
                'quantity': current_quantity,
            }

            existing_entries = details_map.setdefault(original_id, [])
            signature = _detail_merge_signature(details_entry)
            matched = False
            for existing in existing_entries:
                if _detail_merge_signature(existing) == signature:
                    base_qty = _coerce_positive_int(existing.get('quantity'))
                    added_qty = _coerce_positive_int(details_entry.get('quantity'))
                    existing['quantity'] = base_qty + added_qty
                    matched = True
                    break
            if not matched:
                existing_entries.append(details_entry.copy())
            current_quantity = 1

    return details_map

def _iter_detail_pairs(line: str) -> Iterator[Tuple[str, str]]:
    for match in _DETAIL_PAIR_PATTERN.finditer(line):
        label = match.group(1).strip()
        value = match.group(2).strip()
        if not label or not value:
            continue
        yield label, value


def _clean_text_lines(html_content: str) -> List[str]:
    text = _SCRIPT_STYLE_RE.sub(' ', html_content)
    text = _BR_TAG_RE.sub('\n', text)
    text = _BLOCK_CLOSE_RE.sub('\n', text)
    text = _TAG_RE.sub(' ', text)
    text = html.unescape(text)
    text = text.replace('\xa0', ' ')
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _clean_detail_value(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ''
    cleaned = _TRAILING_WS_RE.sub('', cleaned)
    cleaned = _WHITESPACE_RE.sub(' ', cleaned)
    lowered = cleaned.lower()
    if lowered in {'none', 'n/a', 'na', 'no add-ons', 'no addons', 'no add ons', 'no add on'}:
        return ''
    return cleaned


def _format_addon_value(label: str, value: str) -> Optional[Tuple[str, str]]:
    label_clean = re.sub(r'\s+', ' ', label).strip()
    value_clean = value.strip()
    if not value_clean:
        return None
    return (label_clean, value_clean)


def _parse_quantity_value(value: str) -> Optional[int]:
    if value is None:
        return None
    match = re.search(r'\d+', str(value))
    if not match:
        return None
    try:
        quantity = int(match.group())
    except ValueError:
        return None
    return max(1, quantity)


def _coerce_positive_int(value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, (int, float)):
        try:
            return max(1, int(value))
        except ValueError:
            return 1
    match = re.search(r'\d+', str(value))
    if not match:
        return 1
    try:
        return max(1, int(match.group()))
    except ValueError:
        return 1
def _extract_quantity_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r'(?:qty|quantity)\s*[^\d]{0,5}(\d+)', text, re.IGNORECASE)
    if not match:
        return None
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return None


def _parse_identifier_suffix(suffix: str) -> Optional[Dict[str, object]]:
    if not suffix:
        return None

    cleaned = _SMALLTHUMB_RE.sub('', suffix).strip('_')
    if not cleaned:
        return None

    tokens = [token for token in cleaned.split('_') if token]
    if len(tokens) < 3:
        return None

    # Strip image extension for pattern searching, but keep original cleaned for base_id
    base_id = cleaned
    stem = _IMAGE_EXTENSION_RE.sub('', cleaned).rstrip('_')

    # Primary: locate 4-digit + uppercase-letter date code (e.g. 0323A, 0415A).
    # Go back 3 characters from its position to land on the photographer prefix (e.g. 26_).
    # proof_suffix  = stem[start:]          e.g. "26_0323A_25241_OldLibrary"
    # core_id       = proof_suffix[:14]     e.g. "26_0323A_25241"  (original file search key)
    date_match = _ID_DATE_CODE_RE.search(stem)
    if date_match:
        start = max(0, date_match.start() - 3)
        proof_suffix = stem[start:]
        core_id = proof_suffix[:14]
        class_part = stem[:start].strip('_')
        class_tokens = [t for t in class_part.split('_') if t]
        class_descriptor = ' '.join(class_tokens).strip()
        return {
            'sanitized_suffix': stem,
            'base_id': base_id,
            'core_id': core_id,
            'class_tokens': class_tokens,
            'class_descriptor': class_descriptor,
            'proof_suffix': proof_suffix,
        }

    # Fallback: locate first purely-numeric token with 4+ digits as sequence anchor.
    sequence_index = next(
        (idx for idx, token in enumerate(tokens) if _SEQUENCE_NUM_RE.match(token)),
        None,
    )
    if sequence_index is not None and sequence_index >= 2:
        core_start = sequence_index - 2
        class_tokens = tokens[:core_start]
        core_id = '_'.join(tokens[core_start:sequence_index])
        class_descriptor = ' '.join(class_tokens).strip()
        proof_suffix = _strip_image_extension('_'.join(tokens[core_start:]))
    else:
        variant_index = len(tokens)
        for idx, token in enumerate(tokens):
            if token.lower() in {'v', 'h'} and idx >= 3:
                variant_index = idx
                break
        base_tokens = tokens[:variant_index] if len(tokens[:variant_index]) >= 3 else tokens[:3]
        class_tokens = base_tokens[:-3]
        core_id = '_'.join(base_tokens[-3:])
        class_descriptor = ' '.join(class_tokens).strip()
        proof_start = len(class_tokens)
        proof_suffix = _strip_image_extension('_'.join(tokens[proof_start:] or tokens))

    return {
        'sanitized_suffix': stem,
        'base_id': base_id,
        'core_id': core_id,
        'class_tokens': class_tokens,
        'class_descriptor': class_descriptor,
        'proof_suffix': proof_suffix,
    }


def _strip_image_extension(value: str) -> str:
    cleaned = str(value or '').strip().rstrip('_')
    if not cleaned:
        return ''
    cleaned = _IMAGE_EXTENSION_RE.sub('', cleaned).rstrip('_')
    return _TRAILING_SMALLTHUMB_RE.sub('', cleaned).rstrip('_')

def _detail_signature(detail: Optional[Dict[str, object]]) -> Tuple[str, Tuple[str, ...], str, str]:
    if not detail:
        return ('', tuple(), '', '1')
    package = _normalize_signature_value(detail.get('package'))
    background = _normalize_signature_value(detail.get('background'))
    addons = detail.get('addons') or []
    normalized_addons = tuple(
        _normalize_signature_value(
            addon[1]
            if isinstance(addon, tuple)
            else addon.get('value')
            if isinstance(addon, dict)
            else addon
        )
        for addon in addons
        if _normalize_signature_value(
            addon[1]
            if isinstance(addon, tuple)
            else addon.get('value')
            if isinstance(addon, dict)
            else addon
        )
    )
    quantity = _normalize_signature_value(detail.get('quantity') or 1)
    return (package, normalized_addons, background, quantity)


def _detail_merge_signature(detail: Optional[Dict[str, object]]) -> Tuple[str, Tuple[str, ...], str]:
    if not detail:
        return ('', tuple(), '')
    package = _normalize_signature_value(detail.get('package'))
    background = _normalize_signature_value(detail.get('background'))
    addons = detail.get('addons') or []
    normalized_addons = tuple(
        _normalize_signature_value(
            addon[1]
            if isinstance(addon, tuple)
            else addon.get('value')
            if isinstance(addon, dict)
            else addon
        )
        for addon in addons
        if _normalize_signature_value(
            addon[1]
            if isinstance(addon, tuple)
            else addon.get('value')
            if isinstance(addon, dict)
            else addon
        )
    )
    return (package, normalized_addons, background)


@lru_cache(maxsize=1024)
def _normalize_signature_value(value: Optional[str]) -> str:
    if value is None:
        return ''
    value_str = str(value)
    if not value_str:
        return ''
    return _WHITESPACE_RE.sub(' ', value_str).strip().lower()


def extract_class_number(html_content: Optional[str]) -> str:
    if not html_content:
        return 'ZZZ'

    match = _CLASS_NUMBER_RE.search(html_content)
    if match:
        return match.group(1).strip()

    variant_match = _VARIANT_CLASS_RE.search(html_content)
    if variant_match:
        return variant_match.group(1)

    date_match = _DATE_CLASS_RE.search(html_content)
    if date_match:
        return date_match.group(1)

    fallback_match = _FALLBACK_CLASS_RE.search(html_content)
    if fallback_match:
        return fallback_match.group(1)

    return 'ZZZ'


def extract_order_number(html_content: Optional[str]) -> Optional[str]:
    if not html_content:
        return None
    match = _ORDER_NUMBER_RE.search(html_content)
    if not match:
        return None
    return match.group(0).upper()


def sanitize_filename(name: str) -> str:
    sanitized = _SANITIZE_FILENAME_RE.sub('_', name)
    sanitized = sanitized.strip().replace('\n', ' ').replace('\r', '')
    return sanitized or 'Order'


def validate_picture_day_id(picture_day_id: str) -> bool:
    return bool(re.fullmatch(r'[PpHh]\d{7,8}', picture_day_id.strip()))


def parse_picture_day_ids(raw_text: str) -> List[str]:
    tokens = re.split(r'[,\s]+', raw_text.strip())
    picture_day_ids: List[str] = []
    for token in tokens:
        if not token:
            continue
        if not validate_picture_day_id(token):
            raise ValueError(f'Invalid Picture Day ID format: {token}')
        picture_day_ids.append(token.upper())
    return picture_day_ids


def decode_picture_day_date(picture_day_id: str) -> Optional[datetime]:
    match = re.match(r'[PpHh](\d+)', picture_day_id)
    if not match:
        return None

    numeric_portion = match.group(1)

    try:
        divided = int(numeric_portion) / 7
        date_number = int(divided) // 10
        date_str = str(date_number).zfill(6)

        month = int(date_str[0:2])
        day = int(date_str[2:4])
        year = 2000 + int(date_str[4:6])

        return datetime(year, month, day)
    except (ValueError, ZeroDivisionError):
        return None
