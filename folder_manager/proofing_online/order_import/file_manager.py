from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from folder_manager.proofing_online.passwords import strip_proofing_password

from .parsers import sanitize_filename
from .utils import emit_status

_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif'}
_SMALLTHUMB_SUFFIX = '_smallthumb'
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')
_NON_ALNUM_UPPER_RE = re.compile(r'[^A-Za-z0-9]+')
_TRAILING_PUNCT_RE = re.compile(r'[.\s]+$')
_WHITESPACE_RE = re.compile(r'\s+')
_SINGLE_LETTER_DIR_RE = re.compile(r'^[A-Za-z]$')
_PKG_PREFIX_RE = re.compile(r'^(pkg|packages?)\s+', re.IGNORECASE)
_BACKGROUND_PREFIX_RE = re.compile(r'^(backgrounds?|background)\s*[_:,-]*\s*', re.IGNORECASE)
_PHOTODECK_IMPORT_PREFIX = '6. Import Photodeck'
_INVALID_FILENAME_TOKEN_RE = re.compile(r'[<>:"/\\|?*]')
_ADDON_TOKEN_MAP = {
    '110x13': '1-10x13',
    '18x10photocalendar': '1-8x10 Photo Calendar',
    '18x10': '1-8x10',
    '25x7': '2-5x7',
    '44x5': '4-4x5',
    '825x35jumbowallets': '8-2.5x3.5 Jumbo Wallets',
    '23x5photomagnets': '2-3x5 Photo Magnets',
    '1digitalimage': '1 Digital Image',
    '2acrylickeyholderswithpictures': '2-Acrylic Key Holders with Pictures',
    '111x14': '1-11x14',
}
_PACKAGE_TOKEN_MAP = {
    'nopackage': 'PNP',
    'pnp': 'PNP',
    'a': 'PA',
    'packagea': 'PA',
    'pa': 'PA',
    'b': 'PB',
    'packageb': 'PB',
    'pb': 'PB',
    'c': 'PC',
    'packagec': 'PC',
    'pc': 'PC',
    'd': 'PD',
    'packaged': 'PD',
    'pd': 'PD',
    'e': 'PE',
    'packagee': 'PE',
    'pe': 'PE',
    't': 'PT',
    'teacher': 'PT',
    'packageteacher': 'PT',
    'pt': 'PT',
    's': 'PS',
    'sibling': 'PS',
    'packagesibling': 'PS',
    'ps': 'PS',
    '7': 'P7',
    'package7': 'P7',
    'p7': 'P7',
}
_BACKGROUND_TOKEN_MAP = {
    'b_blue': 'B_Blue',
    'b_white': 'B_White',
    'b_burgundy': 'B_Burgundy',
    'b_brown': 'B_Brown',
    'b_cr': 'B_ChildRoom',
    'b_childroom': 'B_ChildRoom',
    'b_childroomgrads': 'B_ChildRoomGrads',
    'b_lamb': 'B_Lamborghini',
    'b_lamborghini': 'B_Lamborghini',
    'b_grey': 'B_Grey',
    'b_ny': 'B_NewYork',
    'b_newyork': 'B_NewYork',
    'b_dubai': 'B_Dubai',
    'b_london': 'B_London',
    'b_centralpark': 'B_CentralPark',
    'b_grngrdn': 'B_GreenGarden',
    'b_greengarden': 'B_GreenGarden',
    'b_arc': 'B_Arc',
    'b_yard': 'B_Yard',
    'b_quietlibrary': 'B_QuietLibrary',
    'b_space': 'B_Space',
    'b_oldlib': 'B_OldLibrary',
    'b_oldlibrary': 'B_OldLibrary',
    'b_lib': 'B_Library',
    'b_library': 'B_Library',
    'b_thanksgiving': 'B_Thanksgiving',
    'b_thepathway': 'B_ThePathway',
    'b_fall15': 'B_Fall15',
    'b_fall': 'B_Fall',
    'b_christmas517': 'B_Christmas517',
    'b_winter5': 'B_Winter5',
    'b_lightedtree': 'B_LightedTree',
    'b_wonderworld': 'B_WonderWorld',
    'b_amsterdam': 'B_Amsterdam',
    'b_maserati': 'B_Maserati',
    'b_migdald': 'B_MigdalD',
    'b_waterf': 'B_WaterFall',
    'b_waterfall': 'B_WaterFall',
    'b_angelst': 'B_AngelStand',
    'b_angelstand': 'B_AngelStand',
    'b_angelstandwinter': 'B_AngelStandWinter',
    'b_beach': 'B_Beach',
    'b_ferrariblue': 'B_FerrariBlue',
    'b_ferrari': 'B_Ferrari',
    'b_ferrarigold': 'B_FerrariGold',
    'b_whitehouse2': 'B_WhiteHouse2',
    'b_whitehouse': 'B_WhiteHouse',
    'b_nycnight': 'B_NYCNight',
    'bh_lamborghiniblue': 'BH_LamborghiniBlue',
    'bh_lamborghini': 'BH_Lamborghini',
    'bh_citynight': 'BH_CityNight',
    'bh_lambiorange': 'BH_LamborghiniOrange',
    'bh_lamborghiniorange': 'BH_LamborghiniOrange',
}
_BACKGROUND_COMPACT_TOKEN_MAP = {
    re.sub(r"[^a-z0-9]+", "", key.lower()): value
    for key, value in _BACKGROUND_TOKEN_MAP.items()
}

_ORDER_PDF_METADATA: Dict[str, Dict[str, object]] = {}


def _normalize_token(value: str) -> str:
    if value is None:
        return ''
    return _NON_ALNUM_RE.sub('', str(value).lower())


def _coerce_quantity(value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, (int, float)):
        qty = int(value)
    else:
        match = re.search(r'\d+', str(value))
        qty = int(match.group()) if match else 1
    return max(1, qty)


def _duplicate_with_quantity(
    source_path: str,
    first_dest_path: Optional[str],
    quantity: int,
    display_folder: str,
    picture_day_id: str,
    item_label: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> None:
    if quantity <= 1 or not first_dest_path or not os.path.exists(source_path):
        return
    base, ext = os.path.splitext(first_dest_path)
    for idx in range(2, quantity + 1):
        duplicate_path = f"{base} ({idx}){ext}"  # Changed from f"{base} {idx}{ext}"
        try:
            if os.path.exists(duplicate_path):
                os.remove(duplicate_path)
            shutil.copy2(source_path, duplicate_path)
            emit_status(
                f"{picture_day_id}:   - Copied {item_label} qty #{idx} to {display_folder}/{os.path.basename(duplicate_path)}",
                progress_callback,
            )
        except Exception as exc:  # pylint: disable=broad-except
            emit_status(
                f"{picture_day_id}:   - Failed to duplicate {item_label} qty #{idx} for {os.path.basename(first_dest_path)} ({exc})",
                progress_callback,
            )
            break


def _has_expected_quantity(first_dest_path: Optional[str], quantity: int) -> bool:
    if not first_dest_path:
        return False
    if quantity <= 1:
        return os.path.exists(first_dest_path)
    base, ext = os.path.splitext(first_dest_path)
    for idx in range(1, quantity + 1):
        suffix = "" if idx == 1 else f" ({idx})"
        candidate = f"{base}{suffix}{ext}"
        if not os.path.exists(candidate):
            return False
    return True


def _find_existing_stage_directory(
    root_directory: str,
    original_id: str,
    target_folder_name: str,
) -> Optional[str]:
    if not original_id or not os.path.isdir(root_directory):
        return None
    original_norm = _normalize_token(original_id)
    if not original_norm:
        return None
    target_norm = _normalize_token(target_folder_name)
    for entry in os.listdir(root_directory):
        entry_path = os.path.join(root_directory, entry)
        if not os.path.isdir(entry_path):
            continue
        entry_norm = _normalize_token(entry)
        if original_norm and original_norm in entry_norm:
            if target_norm and entry_norm == target_norm:
                continue
            return entry_path
    return None


def build_file_index(directory: str, recursive: bool = False) -> List[Dict[str, object]]:
    """
    Walk a directory once and cache metadata needed for identifier lookups.
    """
    if not os.path.exists(directory):
        return []

    index: List[Dict[str, object]] = []
    search_iterable = (
        os.walk(directory)
        if recursive
        else [(directory, [], os.listdir(directory))]
    )

    for current_dir, _, filenames in search_iterable:
        for filename in filenames:
            full_path = os.path.join(current_dir, filename)
            lower_name = filename.lower()
            name_without_ext, ext = os.path.splitext(filename)
            if ext.lower() not in _IMAGE_EXTENSIONS:
                continue
            index.append(
                {
                    'path': full_path,
                    'lower_name': lower_name,
                    'name_without_ext': name_without_ext,
                    'ext': ext,
                    'compact_name': _NON_ALNUM_RE.sub('', name_without_ext.lower()),
                    'is_thumb': name_without_ext.lower().endswith(_SMALLTHUMB_SUFFIX),
                }
            )
        if not recursive:
            break

    return index


def find_file_with_id(
    directory: str,
    file_id: str,
    recursive: bool = False,
    index: Optional[Sequence[Dict[str, object]]] = None,
) -> Optional[str]:
    """
    Locate a file whose name contains the identifier (not just as a prefix).
    Prefers non-thumb images and prioritises prefix matches when possible.
    """
    if (index is None and not os.path.exists(directory)) or not file_id:
        return None

    preferred_file = None
    fallback_file = None

    normalized_id = file_id.lower().rstrip('_')
    normalized_id_no_ext, _ = os.path.splitext(normalized_id)
    normalized_compact = _NON_ALNUM_RE.sub('', normalized_id_no_ext or normalized_id)
    normalized_compact_full = _NON_ALNUM_RE.sub('', normalized_id)

    entries = index if index is not None else build_file_index(directory, recursive)

    for entry in entries:
        ext = entry['ext'].lower()
        if ext not in _IMAGE_EXTENSIONS:
            continue

        lower_name = entry['lower_name']
        compact_name = entry['compact_name']
        lower_name_no_ext = entry['name_without_ext'].lower()

        has_prefix = normalized_id and lower_name.startswith(normalized_id)
        has_prefix_no_ext = normalized_id_no_ext and lower_name_no_ext.startswith(normalized_id_no_ext)
        has_substring = normalized_id and normalized_id in lower_name
        has_substring_no_ext = normalized_id_no_ext and normalized_id_no_ext in lower_name_no_ext
        has_compact_match = (
            (normalized_compact and normalized_compact in compact_name)
            or (normalized_compact_full and normalized_compact_full in compact_name)
        )

        if not (
            has_prefix
            or has_prefix_no_ext
            or has_substring
            or has_substring_no_ext
            or has_compact_match
        ):
            continue

        full_path = entry['path']
        if entry['is_thumb']:
            if fallback_file is None:
                fallback_file = full_path
        else:
            preferred_file = full_path
            break

    return preferred_file or fallback_file


def find_file_with_exact_id(
    directory: str,
    file_id: str,
    recursive: bool = False,
    index: Optional[Sequence[Dict[str, object]]] = None,
) -> Optional[str]:
    """
    Locate a file whose stem matches file_id exactly (case-insensitive).
    Falls back to a compact comparison so separator differences such as
    "_" versus "__" do not prevent matching the intended proof file.
    Returns None if not found; caller is responsible for raising an error.
    """
    if (index is None and not os.path.exists(directory)) or not file_id:
        return None

    target = file_id.lower().rstrip('_')
    target_no_ext = os.path.splitext(target)[0]
    target_compact = _NON_ALNUM_RE.sub('', target_no_ext)

    entries = index if index is not None else build_file_index(directory, recursive)

    for entry in entries:
        if entry['ext'].lower() not in _IMAGE_EXTENSIONS:
            continue
        if entry['name_without_ext'].lower() == target_no_ext:
            return entry['path']

    fallback_file = None
    for entry in entries:
        if entry['ext'].lower() not in _IMAGE_EXTENSIONS:
            continue
        if not target_compact or entry['compact_name'] != target_compact:
            continue
        if entry['is_thumb']:
            if fallback_file is None:
                fallback_file = entry['path']
            continue
        return entry['path']

    return fallback_file


def strip_smallthumb_suffix(filename: str) -> str:
    """Remove '_smallthumb' suffix from filename (case-insensitive) before the extension."""
    name, ext = os.path.splitext(filename)
    suffix = _SMALLTHUMB_SUFFIX
    if name.lower().endswith(suffix):
        return name[:-len(suffix)] + ext
    return filename


def _child_name_from_proof_path(proof_path: str, proofs_folder: str) -> str:
    proof = str(proof_path or "").strip()
    root = str(proofs_folder or "").strip()
    if not proof:
        return ""
    parent = os.path.dirname(proof)
    folder_name = os.path.basename(parent)
    try:
        if root:
            rel_parent = os.path.relpath(parent, root)
            first_part = rel_parent.split(os.sep, 1)[0].strip()
            if first_part and first_part not in {".", ".."}:
                folder_name = first_part
    except Exception:
        pass
    child_name = strip_proofing_password(folder_name)
    child_name = _WHITESPACE_RE.sub(" ", str(child_name or "").strip())
    return child_name


def _display_label_with_child_and_detail(
    filename: str,
    details: Dict[str, object],
    child_name: str,
    *,
    base_override: Optional[str] = None,
) -> str:
    base, _ext = os.path.splitext(filename)
    base = str(base_override or base or "").strip()
    clean_child = sanitize_filename(str(child_name or "").strip()) if child_name else ""
    if clean_child:
        base = f"{base} - {clean_child}" if base else clean_child
    return _apply_detail_suffix(filename, details, base_override=base or base_override)


def _paid_order_detail_key(details: Dict[str, object]) -> tuple:
    addons = tuple(_format_addons((details or {}).get("addons")))
    return (
        _format_package((details or {}).get("package")).lower(),
        addons,
        _format_background((details or {}).get("background")).lower(),
        _coerce_quantity((details or {}).get("quantity")),
    )


def _sanitize_picture_tag_token(value: object) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    cleaned = _INVALID_FILENAME_TOKEN_RE.sub("-", cleaned)
    return cleaned


def _normalize_picture_token_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _portrait_package_token(value: object) -> str:
    cleaned = _sanitize_picture_tag_token(_clean_file_detail(value))
    if not cleaned:
        return "PNP"
    key = _normalize_picture_token_key(cleaned)
    mapped = _PACKAGE_TOKEN_MAP.get(key)
    if mapped:
        return mapped
    return cleaned


def _portrait_background_token(value: object) -> str:
    cleaned = _sanitize_picture_tag_token(_clean_file_detail(value))
    if not cleaned or not _is_meaningful_addon(cleaned):
        return ""
    mapped = _BACKGROUND_TOKEN_MAP.get(cleaned.lower())
    if not mapped:
        mapped = _BACKGROUND_COMPACT_TOKEN_MAP.get(_normalize_picture_token_key(cleaned))
    if mapped:
        return mapped
    return cleaned


def _background_from_proof_id(value: object) -> str:
    stem = os.path.splitext(os.path.basename(str(value or "").strip()))[0]
    if not stem:
        return ""
    match = re.search(r"(?:^|_)B_([^_].*)$", stem, flags=re.IGNORECASE)
    if not match:
        return ""
    return f"B_{match.group(1).strip('_')}"


def _portrait_addon_token(value: object) -> str:
    cleaned = _sanitize_picture_tag_token(_clean_file_detail(value))
    if not cleaned or not _is_meaningful_addon(cleaned):
        return ""
    cleaned = re.sub(r"\s*[xX]\s*", "x", cleaned)
    compact_key = _normalize_picture_token_key(cleaned)
    mapped = _ADDON_TOKEN_MAP.get(compact_key)
    if mapped:
        return mapped
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)
    return cleaned


def _portrait_addon_tokens(addons_value: object) -> List[str]:
    if isinstance(addons_value, dict):
        iterable = [(addons_value.get("label", ""), addons_value.get("value", ""))]
    elif isinstance(addons_value, list):
        iterable = addons_value
    elif isinstance(addons_value, tuple):
        iterable = [addons_value]
    else:
        iterable = []

    tokens: List[str] = []
    seen = set()
    for entry in iterable:
        if isinstance(entry, tuple):
            label, value = entry
        elif isinstance(entry, dict):
            label = entry.get("label", "")
            value = entry.get("value", "")
        else:
            label = ""
            value = entry
        label_clean = _clean_file_detail(label)
        if "digital editing" in label_clean.lower():
            continue
        token = _portrait_addon_token(value)
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _portrait_digital_editing_token(addons_value: object) -> str:
    if isinstance(addons_value, dict):
        iterable = [(addons_value.get("label", ""), addons_value.get("value", ""))]
    elif isinstance(addons_value, list):
        iterable = addons_value
    elif isinstance(addons_value, tuple):
        iterable = [addons_value]
    else:
        iterable = []

    for entry in iterable:
        if isinstance(entry, tuple):
            label, value = entry
        elif isinstance(entry, dict):
            label = entry.get("label", "")
            value = entry.get("value", "")
        else:
            label = ""
            value = entry
        label_clean = _clean_file_detail(label)
        if "digital editing" not in label_clean.lower():
            continue
        cleaned = _sanitize_picture_tag_token(_clean_file_detail(value))
        if not cleaned or not _is_meaningful_addon(cleaned):
            return ""
        return f"DE_{cleaned}"
    return ""


def _portrait_paid_order_tag(details: Dict[str, object], *, proof_id: str = "") -> str:
    package_token = _portrait_package_token((details or {}).get("package"))
    addon_tokens = _portrait_addon_tokens((details or {}).get("addons"))
    digital_editing_token = _portrait_digital_editing_token((details or {}).get("addons"))
    background_token = _portrait_background_token((details or {}).get("background"))
    if not background_token:
        background_token = _portrait_background_token(_background_from_proof_id(proof_id))

    parts: List[str] = [package_token]
    parts.extend(f"+ {addon}" for addon in addon_tokens)
    if digital_editing_token:
        parts.append(digital_editing_token)
    if background_token:
        parts.append(background_token)
    return " ".join(part for part in parts if part).strip()


def _indexed_paid_order_filename(filename: str, duplicate_index: Optional[int]) -> str:
    if not duplicate_index:
        return filename
    stem, ext = os.path.splitext(os.path.basename(str(filename or "").strip()))
    tokens = stem.split("_")
    if len(tokens) >= 3:
        tokens.insert(2, str(int(duplicate_index)))
        return "_".join(tokens) + ext
    return f"{stem}_{int(duplicate_index)}{ext}"


def _original_paid_order_filename(
    source_path: str,
    details: Dict[str, object],
    *,
    proof_id: str = "",
    duplicate_index: Optional[int] = None,
) -> str:
    filename = os.path.basename(str(source_path or "").strip())
    filename = _indexed_paid_order_filename(filename, duplicate_index)
    stem, ext = os.path.splitext(filename)
    tag = _portrait_paid_order_tag(details, proof_id=proof_id)
    if not stem or not tag:
        return filename
    return f"{stem} {tag}{ext}"


def _proof_paid_order_filename(source_path: str, *, duplicate_index: Optional[int] = None) -> str:
    return _indexed_paid_order_filename(os.path.basename(str(source_path or "").strip()), duplicate_index)


def find_matching_subdir(base_folder: str, target_name: str) -> str:
    """
    Locate a subdirectory under base_folder whose normalized name matches target_name,
    ignoring spaces, hyphens, and case.
    """
    expected_path = os.path.join(base_folder, target_name)
    if not os.path.exists(base_folder):
        return expected_path

    normalized_target = _normalize_dir_token(target_name)
    for entry in os.listdir(base_folder):
        full_path = os.path.join(base_folder, entry)
        if not os.path.isdir(full_path):
            continue
        normalized_entry = _normalize_dir_token(entry)
        if normalized_entry == normalized_target:
            return full_path

    return expected_path


_STAGE_PREFIX_RE = re.compile(r'^\s*(\d+)\s*[\.\-_) ]+\s*(.*)$')


def _strip_stage_prefix(value: str) -> tuple[Optional[int], str]:
    match = _STAGE_PREFIX_RE.match(str(value or "").strip())
    if not match:
        return None, str(value or "").strip()
    try:
        stage_number = int(match.group(1))
    except Exception:
        stage_number = None
    return stage_number, match.group(2).strip()


def _find_stage_like_subdir(
    base_folder: str,
    aliases: Sequence[str],
    *,
    stage_number: Optional[int] = None,
    contains_tokens: Sequence[str] = (),
) -> str:
    candidates = [name for name in aliases if name]
    if not candidates:
        return os.path.join(base_folder, "")
    if not os.path.isdir(base_folder):
        return os.path.join(base_folder, candidates[0])

    for name in candidates:
        path = find_matching_subdir(base_folder, name)
        if os.path.isdir(path):
            return path

    normalized_tokens = tuple(
        _normalize_dir_token(token) for token in contains_tokens if str(token or "").strip()
    )
    for entry in os.listdir(base_folder):
        full_path = os.path.join(base_folder, entry)
        if not os.path.isdir(full_path):
            continue
        entry_stage, remainder = _strip_stage_prefix(entry)
        if stage_number is not None and entry_stage != stage_number:
            continue
        normalized_remainder = _normalize_dir_token(remainder or entry)
        if normalized_tokens and not all(token in normalized_remainder for token in normalized_tokens):
            continue
        return full_path

    return os.path.join(base_folder, candidates[0])


def find_originals_subdir(base_folder: str) -> str:
    found = _find_stage_like_subdir(
        base_folder,
        ("Originals", "Original", "1. Original"),
        stage_number=1,
        contains_tokens=("original",),
    )
    if os.path.isdir(found):
        return found
    if not os.path.isdir(base_folder):
        return found

    single_letter_candidates: List[str] = []
    for entry in os.listdir(base_folder):
        full_path = os.path.join(base_folder, entry)
        if not os.path.isdir(full_path):
            continue
        if _SINGLE_LETTER_DIR_RE.match(str(entry or "").strip()):
            single_letter_candidates.append(full_path)

    if not single_letter_candidates:
        return found

    single_letter_candidates.sort(key=lambda path: os.path.basename(path).lower())
    return single_letter_candidates[0]


def find_proofs_subdir(base_folder: str) -> str:
    found = _find_stage_like_subdir(
        base_folder,
        ("Proofs Sorted", "Proofs"),
        stage_number=2,
        contains_tokens=("proof",),
    )
    if os.path.isdir(found) or not os.path.isdir(base_folder):
        return found

    for entry in os.listdir(base_folder):
        full_path = os.path.join(base_folder, entry)
        if not os.path.isdir(full_path):
            continue
        if "proof" in _normalize_dir_token(entry):
            return full_path

    return found


def find_edit_subdir(base_folder: str) -> str:
    return _find_stage_like_subdir(
        base_folder,
        ("3. Edit", "Edit"),
        stage_number=3,
        contains_tokens=("edit",),
    )


def ensure_proofing_paid_order_folders(base_folder: str) -> Dict[str, str]:
    """
    Ensure only the output folders used by the paid-order import exist.
    Source/stage folders are intentionally not created here.
    """
    resolved: Dict[str, str] = {}
    os.makedirs(base_folder, exist_ok=True)
    orders_folder = find_orders_subdir(base_folder)
    order_pdfs_folder = find_matching_subdir(orders_folder, "Order PDFS")
    os.makedirs(order_pdfs_folder, exist_ok=True)
    resolved["orders"] = orders_folder
    resolved["order_pdfs"] = order_pdfs_folder
    return resolved


def find_orders_subdir(base_folder: str) -> str:
    orders_folder = find_matching_subdir(base_folder, "Orders")
    os.makedirs(orders_folder, exist_ok=True)
    return orders_folder


def _ensure_orders_directory(
    base_folder: str,
    preferred_name: str,
    legacy_names: Iterable[str],
) -> str:
    candidates = [preferred_name, *legacy_names]
    for name in candidates:
        path = find_matching_subdir(base_folder, name)
        if os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            return path
    target_path = os.path.join(base_folder, preferred_name)
    os.makedirs(target_path, exist_ok=True)
    return target_path


def register_pdf_metadata(pdf_path: str, metadata: Dict[str, object]) -> None:
    _ORDER_PDF_METADATA[os.path.abspath(pdf_path)] = dict(metadata)


def get_pdf_metadata(pdf_path: str) -> Dict[str, object]:
    return _ORDER_PDF_METADATA.get(os.path.abspath(pdf_path), {}).copy()


def create_photo_order_folders(
    base_folder: str,
    identifiers: List[Dict[str, object]],
    picture_day_id: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """
    For each photo identifier, copy matching originals, proofs, and the corresponding order PDF
    into a folder named "6. Import Photodeck <order number> <base image>" inside the job's 3. Edit folder.
    """
    originals_folder = find_originals_subdir(base_folder)
    proofs_folder = find_proofs_subdir(base_folder)
    originals_exists = os.path.exists(originals_folder)
    proofs_exists = os.path.exists(proofs_folder)

    if not originals_exists:
        emit_status(
            f"{picture_day_id}: Warning - original source folder not found at {originals_folder}",
            progress_callback,
        )

    if not proofs_exists:
        emit_status(
            f"{picture_day_id}: Warning - proof output folder not found at {proofs_folder}",
            progress_callback,
        )

    originals_index = (
        build_file_index(originals_folder, recursive=True) if originals_exists else []
    )
    proofs_index = build_file_index(proofs_folder, recursive=True) if proofs_exists else []

    root_directory = find_edit_subdir(base_folder)
    if root_directory and not os.path.exists(root_directory):
        os.makedirs(root_directory, exist_ok=True)

    processed_folder_paths: List[str] = []
    processed_folder_keys: set[str] = set()

    def remember_folder(path: str) -> None:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in processed_folder_keys:
            return
        processed_folder_keys.add(normalized)
        processed_folder_paths.append(path)

    for identifier in identifiers:
        original_id = identifier['original_id']
        proof_id = identifier['proof_id']
        details = identifier.get('details') or {}
        base_override = identifier.get('detail_base_name')
        class_descriptor = (identifier.get('class_descriptor') or '').strip()
        class_sort_key = identifier.get('__class_sort_key')
        package_sort_key = identifier.get('__package_sort_key')
        sequence_sort_key = identifier.get('__sequence_sort_key')
        source_subject = identifier.get('__source_subject') or ''
        quantity = _coerce_quantity(details.get('quantity'))
        identifier['quantity'] = quantity

        emit_status(
            f"{picture_day_id}:   - Quantity detected for '{original_id}' → {quantity}",
            progress_callback,
        )

        emit_status(
            f"{picture_day_id}: Organizing images for '{original_id}'",
            progress_callback,
        )

        original_file = (
            find_file_with_id(
                originals_folder,
                original_id,
                recursive=True,
                index=originals_index,
            )
            if originals_exists
            else None
        )

        proof_file = (
            find_file_with_id(
                proofs_folder,
                proof_id,
                recursive=True,
                index=proofs_index,
            )
            if proofs_exists
            else None
        )

        pdf_source_path = identifier.get('__pdf_path') or ''
        if not pdf_source_path or not os.path.exists(pdf_source_path):
            raise FileNotFoundError(
                f"{picture_day_id}: PDF source not found for '{original_id}'"
            )

        order_number_value = ''
        if isinstance(identifier.get('order_number'), str):
            order_number_value = identifier['order_number'].strip()

        pdf_base_id = identifier.get('detail_base_name') or original_id
        pdf_base_name = sanitize_filename(pdf_base_id) or sanitize_filename(original_id) or original_id
        target_suffix = (pdf_base_name or original_id).strip() or original_id
        sanitized_order_number = sanitize_filename(order_number_value) if order_number_value else ''
        if sanitized_order_number == 'Order':
            sanitized_order_number = ''
        suffix_parts = [part for part in (sanitized_order_number, target_suffix) if part]
        combined_suffix = ' '.join(suffix_parts) if suffix_parts else target_suffix
        target_folder_name = f"{_PHOTODECK_IMPORT_PREFIX} {combined_suffix}".strip()
        target_folder_path = find_matching_subdir(root_directory, target_folder_name)
        if os.path.exists(target_folder_path):
            raise FileExistsError(
                f"{picture_day_id}: Target import folder already exists for '{original_id}': {target_folder_path}"
            )
        target_display = os.path.basename(target_folder_path)

        pdf_filename = f"{pdf_base_name} - Order.pdf"
        pdf_destination = os.path.join(target_folder_path, pdf_filename)

        original_dest_name = None
        original_dest_path = None
        if original_file:
            original_dest_name = strip_smallthumb_suffix(os.path.basename(original_file))
            original_dest_name = _apply_detail_suffix(original_dest_name, details, base_override)
            original_dest_path = os.path.join(target_folder_path, original_dest_name)

        proof_dest_name = None
        proof_dest_path = None
        if proof_file:
            proof_dest_name = strip_smallthumb_suffix(os.path.basename(proof_file))
            proof_dest_path = os.path.join(target_folder_path, proof_dest_name)

        if not original_file:
            raise FileNotFoundError(
                f"{picture_day_id}: Original not found for '{original_id}'"
            )
        if not proof_file:
            raise FileNotFoundError(
                f"{picture_day_id}: Proof not found for '{proof_id}'"
            )

        os.makedirs(target_folder_path, exist_ok=False)

        shutil.copy2(pdf_source_path, pdf_destination)
        remember_folder(target_folder_path)
        emit_status(
            f"{picture_day_id}:   - Copied PDF to {target_display}/{pdf_filename}",
            progress_callback,
        )

        shutil.copy2(original_file, original_dest_path)
        emit_status(
            f"{picture_day_id}:   - Copied original to {target_display}/{original_dest_name}",
            progress_callback,
        )
        _duplicate_with_quantity(
            original_file,
            original_dest_path,
            quantity,
            target_display,
            picture_day_id,
            'original',
            progress_callback,
        )

        shutil.copy2(proof_file, proof_dest_path)
        emit_status(
            f"{picture_day_id}:   - Copied proof to {target_display}/{proof_dest_name}",
            progress_callback,
        )
        _duplicate_with_quantity(
            proof_file,
            proof_dest_path,
            quantity,
            target_display,
            picture_day_id,
            'proof',
            progress_callback,
        )

        register_pdf_metadata(
            pdf_destination,
            {
                'class_descriptor': class_descriptor,
                'class_sort_key': class_sort_key,
                'package_sort_key': package_sort_key,
                'sequence_sort_key': sequence_sort_key,
                'original_id': original_id,
                'proof_id': proof_id,
                'picture_day_id': picture_day_id,
                'source_pdf': os.path.basename(pdf_source_path),
                'subject': source_subject,
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'quantity': quantity,
                'order_number': order_number_value,
            },
        )

    return processed_folder_paths


def copy_paid_order_assets_to_orders(
    base_folder: str,
    identifiers: List[Dict[str, object]],
    picture_day_id: str,
    order_no: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    proofs_folder_override: Optional[str] = None,
    order_pdf_path: str = "",
) -> List[Dict[str, object]]:
    """
    Copy PhotoDeck originals/proofs into the job Orders folder.
    Original files are renamed with the Select Best portrait tag; proof files keep
    their source filename. UI labels include child/order details for display/search.
    """
    originals_folder = find_originals_subdir(base_folder)
    proofs_folder = str(proofs_folder_override or "").strip() or find_proofs_subdir(base_folder)
    orders_folder = find_orders_subdir(base_folder)

    if not os.path.isdir(originals_folder):
        raise FileNotFoundError(f"{picture_day_id}: Original source folder not found at {originals_folder}")
    if not os.path.isdir(proofs_folder):
        raise FileNotFoundError(f"{picture_day_id}: Proof output folder not found at {proofs_folder}")

    originals_index = build_file_index(originals_folder, recursive=True)
    proofs_index = build_file_index(proofs_folder, recursive=True)
    copied_entries: List[Dict[str, object]] = []
    created_paths: List[str] = []
    seen_asset_keys: set[tuple[object, ...]] = set()
    reserved_paths: set[str] = set()
    normalized_identifiers: List[Dict[str, object]] = []
    duplicate_group_counts: Dict[Tuple[str, str], int] = {}
    for identifier in identifiers:
        original_id = str((identifier or {}).get("original_id") or "").strip()
        proof_id = str((identifier or {}).get("proof_id") or "").strip()
        details = (identifier or {}).get("details") or {}
        group_key = (order_no, original_id, proof_id, *_paid_order_detail_key(details))
        if group_key in seen_asset_keys:
            continue
        seen_asset_keys.add(group_key)
        normalized = dict(identifier or {})
        normalized["original_id"] = original_id
        normalized["proof_id"] = proof_id
        normalized["details"] = details
        normalized_identifiers.append(normalized)
        duplicate_group_counts[(original_id, proof_id)] = duplicate_group_counts.get((original_id, proof_id), 0) + 1
    seen_asset_keys.clear()
    duplicate_group_seen: Dict[Tuple[str, str], int] = {}

    def _reserve_dest_path(filename: str) -> str:
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(orders_folder, filename)
        counter = 2
        while (
            os.path.exists(candidate)
            or os.path.normcase(os.path.abspath(candidate)) in reserved_paths
        ):
            candidate = os.path.join(orders_folder, f"{base} ({counter}){ext}")
            counter += 1
        reserved_paths.add(os.path.normcase(os.path.abspath(candidate)))
        return candidate

    def _copy_one(
        source_path: str,
        asset_type: str,
        original_id: str,
        proof_id: str,
        details: Dict,
        *,
        display_label: str,
        dest_filename: Optional[str] = None,
        child_name: str = "",
    ) -> None:
        dest = _reserve_dest_path(dest_filename or os.path.basename(source_path))
        shutil.copy2(source_path, dest)
        created_paths.append(dest)
        emit_status(
            f"{picture_day_id}:   - Copied {asset_type} to Orders/{os.path.basename(dest)}",
            progress_callback,
        )
        copied_entries.append({
            "path": dest,
            "source_path": source_path,
            "label": display_label or os.path.basename(dest),
            "asset_type": asset_type,
            "original_id": original_id,
            "proof_id": proof_id,
            "package": details.get("package") or "",
            "addons": details.get("addons") or [],
            "background": details.get("background") or "",
            "quantity": _coerce_quantity(details.get("quantity")),
            "child_name": child_name,
            "order_no": order_no,
            "order_pdf_path": order_pdf_path,
            "created": True,
        })

    try:
        for identifier in normalized_identifiers:
            original_id = str(identifier.get("original_id") or "").strip()
            proof_id = str(identifier.get("proof_id") or "").strip()
            details = identifier.get("details") or {}

            group_key = (order_no, original_id, proof_id, *_paid_order_detail_key(details))
            if group_key in seen_asset_keys:
                continue
            seen_asset_keys.add(group_key)
            duplicate_pair_key = (original_id, proof_id)
            duplicate_index: Optional[int] = None
            if duplicate_group_counts.get(duplicate_pair_key, 0) > 1:
                duplicate_group_seen[duplicate_pair_key] = duplicate_group_seen.get(duplicate_pair_key, 0) + 1
                duplicate_index = duplicate_group_seen[duplicate_pair_key]

            original_file = find_file_with_exact_id(
                originals_folder, original_id, recursive=True, index=originals_index,
            )
            if not original_file:
                raise FileNotFoundError(f"{picture_day_id}: Original not found for '{original_id}'")

            proof_file = find_file_with_exact_id(
                proofs_folder, proof_id, recursive=True, index=proofs_index,
            )
            if not proof_file:
                raise FileNotFoundError(f"{picture_day_id}: Proof not found for '{proof_id}'")

            child_name = _child_name_from_proof_path(proof_file, proofs_folder)
            original_display_label = _display_label_with_child_and_detail(
                os.path.basename(original_file),
                details,
                child_name,
                base_override=original_id,
            )
            proof_display_label = _display_label_with_child_and_detail(
                os.path.basename(proof_file),
                details,
                child_name,
                base_override=proof_id,
            )

            _copy_one(
                original_file,
                "original",
                original_id,
                proof_id,
                details,
                display_label=original_display_label,
                dest_filename=_original_paid_order_filename(
                    original_file,
                    details,
                    proof_id=proof_id,
                    duplicate_index=duplicate_index,
                ),
                child_name=child_name,
            )
            _copy_one(
                proof_file,
                "proof",
                original_id,
                proof_id,
                details,
                display_label=proof_display_label,
                dest_filename=_proof_paid_order_filename(proof_file, duplicate_index=duplicate_index),
                child_name=child_name,
            )

        return copied_entries
    except Exception:
        for path in reversed(created_paths):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        raise


def delete_files(file_paths: List[str]) -> None:
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)


__all__ = [
    'build_file_index',
    'find_file_with_id',
    'find_file_with_exact_id',
    'strip_smallthumb_suffix',
    'find_matching_subdir',
    'find_orders_subdir',
    'ensure_proofing_paid_order_folders',
    'create_photo_order_folders',
    'copy_paid_order_assets_to_orders',
    'delete_files',
    'resolve_output_folder',
    'register_pdf_metadata',
    'get_pdf_metadata',
]


def resolve_output_folder(base_directory: str, picture_day_id: str, today: str) -> str:
    os.makedirs(base_directory, exist_ok=True)
    normalized_id = picture_day_id.upper()
    for entry in os.listdir(base_directory):
        full_path = os.path.join(base_directory, entry)
        if os.path.isdir(full_path) and normalized_id in entry.upper():
            return full_path
    folder_name = sanitize_filename(f'{today} {normalized_id}')
    folder_path = os.path.join(base_directory, folder_name)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


def _normalize_dir_token(value: str) -> str:
    return _NON_ALNUM_RE.sub('', value.lower())


def _clean_file_detail(value: Optional[str]) -> str:
    if not value:
        return ''
    cleaned = _WHITESPACE_RE.sub(' ', str(value).strip())
    cleaned = _TRAILING_PUNCT_RE.sub('', cleaned)
    if not cleaned:
        return ''
    lowered = cleaned.lower()
    if lowered in {'none', 'n/a', 'na', 'no add-ons', 'no addons', 'no add ons', 'no add on'}:
        return ''
    return cleaned


def _apply_detail_suffix(
    filename: str,
    details: Dict[str, object],
    base_override: Optional[str] = None,
) -> str:
    base, ext = os.path.splitext(filename)
    base = base_override or base
    package = _format_package(details.get('package'))
    addon_entries = _format_addons(details.get('addons'))
    background = _format_background(details.get('background'))

    parts: List[str] = []

    if package:
        if addon_entries:
            first_addon = addon_entries.pop(0)
            parts.append(f"{package} + {first_addon}")
        else:
            parts.append(package)
    elif addon_entries:
        parts.append(addon_entries.pop(0))

    parts.extend(addon_entries)

    if background:
        parts.append(background)

    if not parts:
        if base_override:
            clean_base = sanitize_filename(base)
            return f"{clean_base}{ext}"
        return filename

    detail_suffix = ' - '.join(parts)
    detailed_base = sanitize_filename(f"{base} - {detail_suffix}")
    return f"{detailed_base}{ext}"


def _format_package(value: Optional[str]) -> str:
    cleaned = _clean_file_detail(value)
    if not cleaned:
        return ''
    if cleaned.lower() in {'no package', 'no packages'}:
        return ''
    cleaned = _PKG_PREFIX_RE.sub('', cleaned)
    if not cleaned:
        return ''
    if not cleaned.lower().startswith('package'):
        cleaned = f"Package {cleaned}"
    package_body = cleaned[len('Package') :].strip()
    abbreviation = _NON_ALNUM_UPPER_RE.sub('', package_body).upper()
    if abbreviation:
        return f"P{abbreviation}"
    return cleaned


def _format_addons(addons_value: object) -> List[str]:
    if isinstance(addons_value, dict):
        iterable = [(addons_value.get('label', ''), addons_value.get('value', ''))]
    elif isinstance(addons_value, list):
        iterable = addons_value
    elif isinstance(addons_value, tuple):
        iterable = [addons_value]
    else:
        iterable = []

    formatted: List[str] = []
    seen = set()

    for entry in iterable:
        if isinstance(entry, tuple):
            label, value = entry
        elif isinstance(entry, dict):
            label = entry.get('label', '')
            value = entry.get('value', '')
        else:
            label = ''
            value = entry

        display = _format_addon_display(label, value)
        if not display:
            continue
        key = display.lower()
        if key in seen:
            continue
        seen.add(key)
        formatted.append(display)

    formatted.sort(key=_addon_sort_key)
    return formatted


def _format_addon_display(label: Optional[str], value: Optional[str]) -> str:
    label_clean = _clean_file_detail(label)
    value_clean = _clean_file_detail(value)
    if not value_clean or not _is_meaningful_addon(value_clean):
        return ''

    if label_clean:
        label_lower = label_clean.lower()
        if label_lower in {'add-ons', 'add ons', 'add-on', 'addons'}:
            return value_clean
        if 'digital editing' in label_lower:
            return value_clean
        if label_lower == value_clean.lower():
            return value_clean
        return f"{label_clean} {value_clean}"

    return value_clean


def _is_meaningful_addon(value: str) -> bool:
    tokens = [token for token in re.split(r'[\s+&/]+', value.lower()) if token]
    if not tokens:
        return False
    if all(token in {'as', 'is'} for token in tokens):
        return False
    return True


def _format_background(value: Optional[str]) -> str:
    cleaned = _clean_file_detail(value)
    if not cleaned:
        return ''
    cleaned = _BACKGROUND_PREFIX_RE.sub('', cleaned)
    return cleaned


def _addon_sort_key(text: str) -> tuple:
    lower = text.lower()
    has_digit = any(char.isdigit() for char in lower)
    looks_like_size = bool(re.search(r'\d\s*x\s*\d', lower)) or 'x' in lower
    if 'as is' in lower:
        priority = 3
    elif has_digit or looks_like_size:
        priority = 0
    elif 'digital' in lower or 'whiten' in lower or 'retouch' in lower or 'edit' in lower:
        priority = 2
    else:
        priority = 1
    return (priority, lower)
