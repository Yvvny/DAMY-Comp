from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pdfplumber
import qrcode
from rapidfuzz import fuzz as rf_fuzz, process as rf_process
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

QR_CODE_SIZE = 80
TEXT_OFFSET_X = 90
TEXT_OFFSET_Y = 15
HORIZONTAL_MARGIN = 20
VERTICAL_MARGIN = 40
NUM_COLUMNS = 2
NUM_ROWS = 5
MAX_TEXT_WIDTH = 100
PRESET = 60
UPSHIFT = 30


@dataclass(frozen=True)
class OrderItem:
    package: str
    background: str
    pose: str
    class_teacher: str
    add_ons: str
    amount: str
    page: int
    source_email: str = ""


@dataclass(frozen=True)
class StudentRow:
    sheet_name: str
    name: str
    clazz: str
    password: str


@dataclass(frozen=True)
class QRGenerationResult:
    mode: str
    output_pdf_path: str
    manifest_path: str
    total_tags: int
    matched_order_tags: int
    parsed_order_items: int
    parsed_order_names: int
    students_without_orders: int
    unmatched_pdf_names: tuple[str, ...]


POSE_CODE_RULES = (
    (re.compile(r"\bhead\s*(?:and|&)?\s*shoulder(?:s)?\b", re.IGNORECASE), "HAS"),
    (re.compile(r"\bhands?\s*on\s*chin\b", re.IGNORECASE), "HOC"),
    (re.compile(r"\bhands?\s*on\s*hip(?:s)?\b", re.IGNORECASE), "HOH"),
    (re.compile(r"\bportrait\b", re.IGNORECASE), "PRT"),
)


def draw_wrapped_text(c, text, x, y, max_width, *, font_name="Helvetica-Bold", font_size=12):
    line_height = 1.2 * font_size
    words = re.split(r"[ -]", text) if text else [""]
    current_line = words[0]
    for word in words[1:]:
        candidate = f"{current_line} {word}".strip()
        if c.stringWidth(candidate, fontName=font_name, fontSize=font_size) < max_width:
            current_line = candidate
        else:
            c.drawString(x, y, current_line)
            y -= line_height
            current_line = word
    c.drawString(x, y, current_line)


def _wrap_text_to_lines(c, text: str, max_width: float, *, font_name="Helvetica-Bold", font_size=12) -> list[str]:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    if not s:
        return []

    words = s.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if c.stringWidth(candidate, fontName=font_name, fontSize=font_size) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        # If a single token is wider than max_width, split by character.
        if c.stringWidth(word, fontName=font_name, fontSize=font_size) > max_width:
            chunk = ""
            for ch in word:
                ch_candidate = f"{chunk}{ch}"
                if c.stringWidth(ch_candidate, fontName=font_name, fontSize=font_size) <= max_width:
                    chunk = ch_candidate
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            current = chunk
        else:
            current = word

    if current:
        lines.append(current)
    return lines


def _fit_text_to_width(c, text: str, max_width: float, *, font_name="Helvetica-Bold", font_size=12) -> str:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    if not s:
        return ""
    if c.stringWidth(s, fontName=font_name, fontSize=font_size) <= max_width:
        return s

    ellipsis = "..."
    ellipsis_w = c.stringWidth(ellipsis, fontName=font_name, fontSize=font_size)
    if ellipsis_w >= max_width:
        return ""

    hi = len(s)
    while hi > 0 and c.stringWidth(s[:hi] + ellipsis, fontName=font_name, fontSize=font_size) > max_width:
        hi -= 1
    return (s[:hi].rstrip() + ellipsis) if hi > 0 else ""


def draw_fitted_text(c, text, x, y, max_width, *, font_name="Helvetica-Bold", font_size=12):
    c.setFont(font_name, font_size)
    c.drawString(x, y, _fit_text_to_width(c, text, max_width, font_name=font_name, font_size=font_size))


def draw_dotted_line(c, x1, y1, x2, y2, dot_length=0.05 * inch, space_length=0.03 * inch):
    c.saveState()
    c.setDash([dot_length, space_length], 0)
    c.line(x1, y1, x2, y2)
    c.restoreState()


def create_qr_code(data: str):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=20,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white")


def generate_password(student_name: str) -> str:
    name_hash = hashlib.sha256(student_name.encode("utf-8")).hexdigest()
    random_number = int(name_hash[:4], 16) % 10000
    return f"{random_number:04d}"


def normalize_class_for_qr(class_text: str) -> str:
    s = (class_text or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "", s)
    if s.lower().startswith("class"):
        s = s[5:]
    return f"Class{s}"


def extract_class_from_order(class_teacher_text: str, fallback_class: str = "") -> str:
    src = (class_teacher_text or "").strip()
    match = re.search(
        r"\bclass\b\s*[:\-]?\s*([a-z0-9]+(?:[-/][a-z0-9]+)?)",
        src,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_class_for_qr(match.group(1))
    return normalize_class_for_qr(fallback_class)


def extract_package_code(package_text: str) -> str:
    pkg = (package_text or "").strip()
    match = re.search(r"\bpackage\s*([a-z0-9]+)\b", pkg, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def normalize_qr_text(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)
    cleaned = re.sub(r"\s*:\s*", " ", cleaned)
    cleaned = re.sub(r"\s*,\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def normalize_package_label_for_qr(package_text: str, add_ons: str = "") -> str:
    cleaned = (package_text or "").strip()
    add_on_text = (add_ons or "").strip()
    if add_on_text:
        cleaned = re.sub(
            rf",\s*add on:\s*{re.escape(add_on_text)}\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    return normalize_qr_text(cleaned)


def extract_pose_code(pose_text: str) -> str:
    raw = (pose_text or "").strip()
    if not raw:
        return "PRT"
    for pattern, code in POSE_CODE_RULES:
        if pattern.search(raw):
            return code
    cleaned = re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")
    return cleaned or "PRT"


def normalize_money_text(raw_amount: str) -> str:
    raw = (raw_amount or "").strip()
    if not raw:
        return ""
    m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", raw)
    if not m:
        return ""
    numeric = m.group(0).replace(",", "")
    try:
        return f"${float(numeric):.2f}"
    except Exception:
        return f"${numeric}"


def build_roster_qr_content(name: str, class_text: str, password: str) -> str:
    safe_password = (password or "").strip()
    return f"{name}[{class_text}]_{safe_password}_"


def build_order_qr_content(
    name: str,
    class_text: str,
    password: str,
    package_code: str,
    package_text: str,
    add_ons: str,
    background_text: str,
    pose_code: str,
) -> str:
    safe_password = (password or "").strip()
    fp_tag = f"FP{package_code}" if package_code else "FP"
    detail_parts = [fp_tag]

    if package_code:
        add_on_tag = normalize_qr_text(add_ons)
        if add_on_tag:
            detail_parts.append(f"+ {add_on_tag}")
    else:
        package_label = normalize_package_label_for_qr(package_text, add_ons)
        if package_label:
            detail_parts.append(package_label)
        add_on_tag = normalize_qr_text(add_ons)
        if add_on_tag:
            detail_parts.append(f"+ {add_on_tag}")

    background_tag = normalize_qr_text(background_text)
    if background_tag:
        detail_parts.append(background_tag)

    detail_parts.append(pose_code)
    order_detail = " ".join(part for part in detail_parts if part)
    qr_parts = [part for part in (safe_password, order_detail) if part]
    return f"{name}[{class_text}]_" + "_".join(qr_parts)


def _looks_like_header_row(first_cell: str, second_cell: str, third_cell: str = "") -> bool:
    first = re.sub(r"\s+", " ", (first_cell or "").strip().lower())
    second = re.sub(r"\s+", " ", (second_cell or "").strip().lower())
    third = re.sub(r"\s+", " ", (third_cell or "").strip().lower())

    name_headers = {"child name", "name", "student name"}
    class_headers = {"class", "class name", "class/teacher", "class teacher", "teacher", "homeroom"}
    password_headers = {"password", "pwd", "passcode"}

    if first not in name_headers:
        return False
    if second in password_headers:
        return True
    return second in class_headers and third in password_headers


def _cell_to_text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_password_text(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit() and len(text) < 4:
        return text.zfill(4)
    return text


def _read_excel_like(path: Path) -> list[StudentRow]:
    suffix = path.suffix.lower()
    rows: list[StudentRow] = []
    if suffix == ".csv":
        csv_df = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
        frames = {path.stem: csv_df}
    else:
        frames = pd.read_excel(path, sheet_name=None, header=None, dtype=str, keep_default_na=False)

    for sheet_name, df in frames.items():
        for _, row in df.iterrows():
            if len(row) < 1 or pd.isna(row[0]):
                continue
            name = _cell_to_text(row[0])
            if not name:
                continue
            class_value = _cell_to_text(row[1]) if len(row) > 1 else ""
            password_value = _cell_to_text(row[2]) if len(row) > 2 else ""
            if _looks_like_header_row(name, class_value, password_value):
                continue
            password = _normalize_password_text(password_value) or generate_password(name)
            rows.append(
                StudentRow(
                    sheet_name=str(sheet_name),
                    name=name,
                    clazz=class_value or str(sheet_name).strip(),
                    password=password,
                )
            )
    return rows


def _extract_child_name_from_order_line(line: str) -> str | None:
    raw = re.sub(r"\s+", " ", str(line or "").strip())
    if not raw:
        return None

    m = re.match(r"(?i)^child\s*name\s*:\s*(.+)$", raw)
    if m:
        name = re.sub(r"\s+", " ", (m.group(1) or "").strip())
        return name.lower() if name else None

    m = re.match(r"(?i)^child\s*last\s*name\s*,?\s*first\s*name\s*:\s*(.+)$", raw)
    if not m:
        return None
    value = re.sub(r"\s+", " ", (m.group(1) or "").strip())
    if not value:
        return None
    if "," in value:
        left, right = value.split(",", 1)
        last = re.sub(r"\s+", " ", left.strip())
        first = re.sub(r"\s+", " ", right.strip())
        merged = f"{first} {last}".strip()
        return re.sub(r"\s+", " ", merged).lower() if merged else None
    return value.lower()


def _extract_first_email_from_lines(lines: list[str]) -> str:
    email_regex = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    for line in lines:
        match = email_regex.search(str(line or ""))
        if match:
            return (match.group(0) or "").strip()
    return ""


def parse_orders_pdf(pdf_path: str | os.PathLike[str]) -> dict[str, list[OrderItem]]:
    orders: dict[str, list[OrderItem]] = {}
    package_line_regex = re.compile(
        r"(package\s+[a-z0-9]+|picture\s+by\s+designer:|school\s+graduates\s+package\s+[a-z0-9]+)",
        re.IGNORECASE,
    )
    ignore_summary_regex = re.compile(r"^packages?\b.*(subtotal|total|summary)", re.IGNORECASE)
    class_teacher_regex = re.compile(
        r"\bclass\s*/?\s*teacher\s*(info|name)?\b|\bclass\s*name\b|\bteacher\s*name\b",
        re.IGNORECASE,
    )
    add_ons_regex = re.compile(r"\badd\s*[- ]?on[s]?\b", re.IGNORECASE)
    money_regex = re.compile(r"\$\s*\d[\d,]*(?:\.\d{2})?")
    money_ignore_regex = re.compile(r"\b(subtotal|order\s*total|shipping|tax|total)\b", re.IGNORECASE)

    def commit_item(
        items: list[OrderItem],
        pkg: str,
        bg: str,
        pose: str,
        cls: str,
        add_ons: str,
        amount: str,
        page_num: int,
        source_email: str,
    ):
        if not pkg:
            return
        package_out = (pkg or "").strip()
        if add_ons:
            package_out = f"{package_out}, Add On: {add_ons.strip()}"
        items.append(
            OrderItem(
                package=package_out,
                background=(bg or "").strip(),
                pose=(pose or "").strip(),
                class_teacher=(cls or "").strip(),
                add_ons=(add_ons or "").strip(),
                amount=(amount or "").strip(),
                page=int(page_num),
                source_email=(source_email or "").strip(),
            )
        )

    with pdfplumber.open(str(pdf_path)) as pdf:
        first_email = ""
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not first_email:
                first_email = _extract_first_email_from_lines(lines)
            child_names_by_idx: dict[int, str] = {}
            for idx, line in enumerate(lines):
                parsed_name = _extract_child_name_from_order_line(line)
                if parsed_name:
                    child_names_by_idx[idx] = parsed_name
            child_indices = sorted(child_names_by_idx.keys())

            for pos, i in enumerate(child_indices):
                child_name = child_names_by_idx[i]
                items = orders.setdefault(child_name, [])
                prev_child_idx = child_indices[pos - 1] if pos > 0 else -1
                next_child_idx = child_indices[pos + 1] if (pos + 1) < len(child_indices) else len(lines)

                pkg = bg = pose = cls = amount = ""
                add_on_values_reversed: list[str] = []

                j = i - 1
                while j > prev_child_idx:
                    raw = lines[j]
                    lower = raw.lower()
                    if ignore_summary_regex.search(lower):
                        j -= 1
                        continue
                    if not amount and ("$" in raw) and not money_ignore_regex.search(lower):
                        money_match = money_regex.search(raw)
                        if money_match:
                            amount = normalize_money_text(money_match.group(0))
                            j -= 1
                            continue
                    if not pkg and package_line_regex.search(lower):
                        pkg = raw.strip()
                        j -= 1
                        continue
                    if ("background" in lower) and (":" in raw) and not bg:
                        bg = raw.split(":", 1)[1].strip()
                        j -= 1
                        continue
                    if ("pose" in lower) and (":" in raw) and not pose:
                        pose = raw.split(":", 1)[1].strip()
                        j -= 1
                        continue
                    if add_ons_regex.search(lower) and (":" in raw):
                        add_on_value = raw.split(":", 1)[1].strip()
                        if add_on_value:
                            add_on_values_reversed.append(add_on_value)
                        j -= 1
                        continue
                    j -= 1

                for j in range(i + 1, next_child_idx):
                    raw = lines[j]
                    lower = raw.lower()
                    if ignore_summary_regex.search(lower):
                        continue
                    if package_line_regex.search(lower):
                        continue
                    if ":" in raw:
                        label, value = raw.split(":", 1)
                        label_lower = label.lower()
                        if class_teacher_regex.search(label_lower):
                            parsed_cls = value.strip()
                            if parsed_cls:
                                cls = parsed_cls

                add_ons = " | ".join(reversed(add_on_values_reversed))
                commit_item(items, pkg, bg, pose, cls, add_ons, amount, page_num, first_email)

    return orders


def parse_orders_pdfs(pdf_paths: list[str | os.PathLike[str]]) -> dict[str, list[OrderItem]]:
    merged: dict[str, list[OrderItem]] = {}
    for raw in pdf_paths:
        path = Path(raw).expanduser().resolve()
        parsed = parse_orders_pdf(path)
        for child_name, items in parsed.items():
            merged.setdefault(child_name, []).extend(items)
    return merged


def _draw_orders_page_counter(c, width: float, height: float, page_index: int, total_pages: int) -> None:
    if total_pages <= 0:
        return
    c.saveState()
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.grey)
    c.drawRightString(width - HORIZONTAL_MARGIN, 14, f"{page_index}/{total_pages}")
    c.restoreState()


def fuzzy_match_name_indices(
    list_a: list[str],
    list_b: list[str],
    threshold: int = 85,
) -> dict[int, int | None]:
    import unicodedata

    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}

    def normalize(text: str) -> str:
        text = text or ""
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.lower().strip()
        text = re.sub(r"[^\w\s\-']", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text

    def tokenize(text: str) -> list[str]:
        parts: list[str] = []
        for token in normalize(text).split():
            parts.extend(token.split("-"))
        return [part for part in parts if part]

    def is_initial(token: str) -> bool:
        return len(token.strip(".").lower()) == 1

    def surname_tokens(tokens: list[str]) -> tuple[str, ...]:
        out = []
        for token in reversed(tokens):
            lowered = token.strip(".").lower()
            if not lowered or is_initial(lowered) or lowered in suffixes:
                continue
            out.append(lowered)
            if len(out) == 2:
                break
        out.reverse()
        return tuple(out) if out else tuple(tokens[-1:])

    normalized_a = [normalize(x) for x in list_a]
    normalized_b = [normalize(x) for x in list_b]
    if not normalized_a:
        return {}
    if not normalized_b:
        return {i: None for i in range(len(normalized_a))}
    base_scores = rf_process.cdist(normalized_a, normalized_b, scorer=rf_fuzz.token_set_ratio)

    tokens_a = [tokenize(x) for x in list_a]
    tokens_b = [tokenize(x) for x in list_b]

    def surnames_match(tokens_left: list[str], tokens_right: list[str]) -> bool:
        for left in surname_tokens(tokens_left):
            for right in surname_tokens(tokens_right):
                if left == right or rf_fuzz.ratio(left, right) >= 95:
                    return True
        return False

    for i in range(len(normalized_a)):
        for j in range(len(normalized_b)):
            score = base_scores[i][j]
            if surnames_match(tokens_a[i], tokens_b[j]):
                score = min(100, score + 6)
            elif score >= 92:
                score -= 5
            base_scores[i][j] = score

    candidates: list[tuple[int, int, float]] = []
    for i, row in enumerate(base_scores):
        for j, score in enumerate(row):
            if score >= threshold:
                candidates.append((i, j, score))
    candidates.sort(key=lambda item: item[2], reverse=True)

    used_i = set()
    used_j = set()
    mapping_idx: dict[int, int | None] = {}
    for i, j, _score in candidates:
        if i in used_i or j in used_j:
            continue
        mapping_idx[i] = j
        used_i.add(i)
        used_j.add(j)

    for i in range(len(list_a)):
        mapping_idx.setdefault(i, None)
    return mapping_idx


def fuzzy_match_names(list_a: list[str], list_b: list[str], threshold: int = 85) -> dict[str, str | None]:
    """
    Backward-compatible name mapping helper.
    NOTE: For duplicate names, key collisions can occur; prefer fuzzy_match_name_indices.
    """
    idx_map = fuzzy_match_name_indices(list_a, list_b, threshold=threshold)
    out: dict[str, str | None] = {}
    for i, value in idx_map.items():
        left = list_a[i]
        out[left] = list_b[value] if value is not None else None
    for item in list_a:
        out.setdefault(item, None)
    return out


def _render_order_tag(
    c,
    width,
    height,
    base_name: str,
    row_number: int,
    tag_number: int,
    sheet_name: str,
    student: StudentRow,
    item: OrderItem,
    detail_note: str,
):
    row_h = height / NUM_ROWS
    row_w = width - 2 * HORIZONTAL_MARGIN

    x_left = HORIZONTAL_MARGIN
    x_right = width - HORIZONTAL_MARGIN - QR_CODE_SIZE
    y = height - VERTICAL_MARGIN - row_number * row_h - QR_CODE_SIZE

    qr_class = extract_class_from_order(item.class_teacher, student.clazz)
    package_code = extract_package_code(item.package)
    pose_code = extract_pose_code(item.pose)
    qr_content = build_order_qr_content(
        student.name,
        qr_class,
        student.password,
        package_code,
        item.package,
        item.add_ons,
        item.background,
        pose_code,
    )
    qr_img = create_qr_code(qr_content).convert("RGB")
    img_buf = ImageReader(qr_img)

    c.drawImage(img_buf, x_left, y + UPSHIFT, width=QR_CODE_SIZE, height=QR_CODE_SIZE)
    c.drawImage(img_buf, x_right, y + UPSHIFT, width=QR_CODE_SIZE, height=QR_CODE_SIZE)

    text_x = x_left + QR_CODE_SIZE + 10
    text_max_w = row_w - 2 * QR_CODE_SIZE - 20
    draw_fitted_text(c, base_name[:32], text_x, y + PRESET + TEXT_OFFSET_Y * 3, text_max_w, font_size=12)
    draw_fitted_text(c, sheet_name[:48], text_x, y + PRESET + TEXT_OFFSET_Y * 2, text_max_w, font_size=12)
    draw_fitted_text(c, f"{tag_number}. {student.name}", text_x, y + TEXT_OFFSET_Y + PRESET, text_max_w, font_size=12)

    c.setFillColor(colors.black)
    detail_top_y = y - 25 + PRESET
    dotted_y = y - QR_CODE_SIZE / 2
    detail_bottom_y = dotted_y + 2

    detail_rows = [
        (item.pose, False),
        (item.background, False),
        (item.class_teacher, False),
        (detail_note or "", False),
    ]
    detail_texts = [txt for txt, _ in detail_rows if str(txt or "").strip()]

    selected_font = 8
    selected_line_h = 10
    selected_package_lines: list[str] = _wrap_text_to_lines(
        c, item.package, text_max_w, font_name="Helvetica-Bold", font_size=selected_font
    )
    for candidate_font in (10, 9, 8):
        candidate_line_h = candidate_font + 2
        candidate_pkg = _wrap_text_to_lines(
            c, item.package, text_max_w, font_name="Helvetica-Bold", font_size=candidate_font
        )
        total_lines = len(candidate_pkg) + len(detail_texts)
        available_lines = max(1, int((detail_top_y - detail_bottom_y) // candidate_line_h) + 1)
        if total_lines <= available_lines:
            selected_font = candidate_font
            selected_line_h = candidate_line_h
            selected_package_lines = candidate_pkg
            break

    c.setFont("Helvetica-Bold", selected_font)
    cursor_y = detail_top_y
    for line in selected_package_lines:
        c.drawString(text_x, cursor_y, line)
        cursor_y -= selected_line_h

    for value in detail_texts:
        c.drawString(text_x, cursor_y, value)
        cursor_y -= selected_line_h

    if item.amount:
        c.setFont("Helvetica-Bold", 11)
        # Keep price inside the tag content area (above the dotted separator line).
        c.drawRightString(width - HORIZONTAL_MARGIN, y - 85 + PRESET, item.amount)

    draw_dotted_line(c, HORIZONTAL_MARGIN, dotted_y, width - HORIZONTAL_MARGIN, dotted_y)
    return qr_content


def _render_roster_tag(c, width, height, base_name: str, cell_idx: int, tag_number: int, sheet_name: str, student: StudentRow):
    col = cell_idx % NUM_COLUMNS
    row_number = (cell_idx // NUM_COLUMNS) % NUM_ROWS
    x = HORIZONTAL_MARGIN + col * (width / NUM_COLUMNS)
    y = height - VERTICAL_MARGIN - row_number * (height / NUM_ROWS) - QR_CODE_SIZE

    qr_class = normalize_class_for_qr(student.clazz)
    qr_content = build_roster_qr_content(student.name, qr_class, student.password)
    qr_img = create_qr_code(qr_content).convert("RGB")
    img_buf = ImageReader(qr_img)

    c.drawImage(img_buf, x, y + UPSHIFT, width=QR_CODE_SIZE, height=QR_CODE_SIZE)
    right_qr_x = x + TEXT_OFFSET_X + MAX_TEXT_WIDTH + 5
    c.drawImage(
        img_buf,
        right_qr_x,
        y + UPSHIFT,
        width=QR_CODE_SIZE,
        height=QR_CODE_SIZE,
    )

    text_x = x + TEXT_OFFSET_X
    text_max_w = max(20, right_qr_x - text_x - 5)
    draw_fitted_text(c, base_name[:32], text_x, y + PRESET + TEXT_OFFSET_Y * 3, text_max_w, font_size=12)
    draw_fitted_text(c, sheet_name[:48], text_x, y + PRESET + TEXT_OFFSET_Y * 2, text_max_w, font_size=12)
    draw_fitted_text(c, f"{tag_number}. {student.name}", text_x, y + TEXT_OFFSET_Y + PRESET, text_max_w, font_size=12)

    if (cell_idx + 1) % NUM_COLUMNS != 0:
        usable_w = width - 2 * HORIZONTAL_MARGIN
        usable_h = height - 2 * VERTICAL_MARGIN
        col_w = usable_w / NUM_COLUMNS
        row_h = usable_h / NUM_ROWS
        x_mid = HORIZONTAL_MARGIN + col_w
        row_top = height - VERTICAL_MARGIN - (row_number * row_h)
        row_bottom = row_top - row_h
        draw_dotted_line(c, x_mid, row_bottom, x_mid, row_top)

    dotted_y = y - QR_CODE_SIZE / 2
    draw_dotted_line(c, HORIZONTAL_MARGIN, dotted_y, width - HORIZONTAL_MARGIN, dotted_y)
    return qr_content


def generate_qr_tags(
    *,
    excel_path: str | os.PathLike[str] | None,
    pdf_path: str | os.PathLike[str] | None,
    output_dir: str | os.PathLike[str],
    mode: str,
    pdf_paths: list[str | os.PathLike[str]] | None = None,
    output_base_name: str | None = None,
) -> QRGenerationResult:
    mode = str(mode or "").strip().lower()
    if mode not in {"roster", "orders"}:
        raise ValueError("mode must be 'roster' or 'orders'")

    excel_file: Path | None = None
    if excel_path:
        excel_file = Path(excel_path).expanduser().resolve()
        if not excel_file.exists():
            raise FileNotFoundError(f"Excel file not found: {excel_file}")

    pdf_files: list[Path] = []
    if pdf_paths:
        for raw in pdf_paths:
            path = Path(raw).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"PDF file not found: {path}")
            pdf_files.append(path)
    if pdf_path:
        single = Path(pdf_path).expanduser().resolve()
        if not single.exists():
            raise FileNotFoundError(f"PDF file not found: {single}")
        pdf_files.append(single)
    if pdf_files:
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in pdf_files:
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        pdf_files = deduped
    if mode == "orders" and not pdf_files:
        raise ValueError("QR Orders requires at least one PDF file.")

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    students: list[StudentRow] = []
    if excel_file is not None:
        students = _read_excel_like(excel_file)
        if mode == "roster" and not students:
            raise ValueError("No student rows were found in the selected Excel file.")
    elif mode == "roster":
        raise ValueError("QR Roster requires an Excel file.")

    orders_dict = parse_orders_pdfs([str(p) for p in pdf_files]) if pdf_files else {}
    parsed_order_items = sum(len(items) for items in orders_dict.values())

    excel_names = [row.name.lower() for row in students]
    pdf_names = list(orders_dict.keys())
    if pdf_names and excel_names:
        pdf_to_excel_idx = fuzzy_match_name_indices(pdf_names, excel_names)
    elif pdf_names:
        pdf_to_excel_idx = {idx: None for idx in range(len(pdf_names))}
    else:
        pdf_to_excel_idx = {}
    excel_to_pdf_idx = (
        fuzzy_match_name_indices(excel_names, pdf_names)
        if (pdf_names and excel_names)
        else {idx: None for idx in range(len(excel_names))}
    )

    by_sheet: dict[str, list[tuple[StudentRow, str | None, list[OrderItem], bool]]] = defaultdict(list)
    for idx, student in enumerate(students):
        match_idx = excel_to_pdf_idx.get(idx)
        match_name = pdf_names[match_idx] if (match_idx is not None and 0 <= match_idx < len(pdf_names)) else None
        items = orders_dict.get(match_name, []) if match_name else []
        by_sheet[student.sheet_name].append((student, match_name, items, True))

    if mode == "orders":
        unmatched_names_for_orders = [
            pdf_names[idx]
            for idx, matched_excel_idx in pdf_to_excel_idx.items()
            if matched_excel_idx is None and 0 <= idx < len(pdf_names)
        ]
        if unmatched_names_for_orders:
            fallback_sheet = "PDF Orders"
            for pdf_name in unmatched_names_for_orders:
                items = orders_dict.get(pdf_name, [])
                if not items:
                    continue
                fallback_student = StudentRow(
                    sheet_name=fallback_sheet,
                    name=str(pdf_name or "").strip(),
                    clazz="",
                    password=generate_password(str(pdf_name or "").strip()),
                )
                by_sheet[fallback_sheet].append((fallback_student, pdf_name, items, False))

    if excel_file is not None:
        base_name = excel_file.stem
    elif pdf_files:
        base_name = pdf_files[0].stem
    else:
        base_name = "qr_orders"
    output_stem = str(output_base_name or "").strip() or f"{base_name}_qr_{mode}"
    output_pdf_path = out_dir / f"{output_stem}.pdf"
    manifest_path = out_dir / f"{output_stem}.txt"

    sheet_sequence = list(dict.fromkeys(row.sheet_name for row in students).keys())
    if mode == "orders" and "PDF Orders" in by_sheet:
        sheet_sequence.append("PDF Orders")
    per_sheet_render_rows: list[tuple[str, list[tuple[StudentRow, str, OrderItem, str] | StudentRow]]] = []
    for sheet_name in sheet_sequence:
        sheet_rows = by_sheet.get(sheet_name, [])
        order_rows: list[tuple[StudentRow, str, OrderItem, str]] = []
        roster_rows: list[StudentRow] = []
        for student, match_name, items, matched_from_excel in sheet_rows:
            if items:
                for item in items:
                    fallback_email = (getattr(item, "source_email", "") or "").strip()
                    if not matched_from_excel and fallback_email:
                        detail_note = f"Order email: {fallback_email}"
                    else:
                        detail_note = (match_name or "").strip()
                    order_rows.append((student, match_name or "", item, detail_note))
            elif mode == "roster":
                roster_rows.append(student)
        render_rows: list[tuple[StudentRow, str, OrderItem, str] | StudentRow] = (
            order_rows if mode == "orders" else order_rows + roster_rows
        )
        if render_rows:
            per_sheet_render_rows.append((sheet_name, render_rows))

    if not per_sheet_render_rows:
        raise ValueError("No QR tags were created. Check that the PDF and Excel names match.")

    flat_render_rows: list[tuple[str, tuple[StudentRow, str, OrderItem, str] | StudentRow]] = []
    for sheet_name, rows in per_sheet_render_rows:
        for row in rows:
            flat_render_rows.append((sheet_name, row))

    total_orders_pages = 0
    if mode == "orders":
        total_order_tags = sum(1 for _sheet_name, row in flat_render_rows if isinstance(row, tuple))
        total_orders_pages = (total_order_tags + NUM_ROWS - 1) // NUM_ROWS

    c = canvas.Canvas(str(output_pdf_path), pagesize=letter)
    width, height = letter
    tag_number = 0
    matched_order_tags = 0
    students_without_orders = 0
    current_output_page = 1 if flat_render_rows else 0

    with open(manifest_path, "w", encoding="utf-8") as manifest:
        cell_idx = 0
        for sheet_name, row in flat_render_rows:
            if cell_idx and cell_idx % (NUM_COLUMNS * NUM_ROWS) == 0:
                if mode == "orders":
                    _draw_orders_page_counter(c, width, height, current_output_page, total_orders_pages)
                c.showPage()
                current_output_page += 1

            if isinstance(row, tuple):
                student, _match_name, item, detail_note = row
                if cell_idx % NUM_COLUMNS != 0:
                    cell_idx += (NUM_COLUMNS - (cell_idx % NUM_COLUMNS))
                    if cell_idx and cell_idx % (NUM_COLUMNS * NUM_ROWS) == 0:
                        if mode == "orders":
                            _draw_orders_page_counter(c, width, height, current_output_page, total_orders_pages)
                        c.showPage()
                        current_output_page += 1
                row_number = (cell_idx // NUM_COLUMNS) % NUM_ROWS
                tag_number += 1
                qr_content = _render_order_tag(
                    c,
                    width,
                    height,
                    base_name,
                    row_number,
                    tag_number,
                    sheet_name,
                    student,
                    item,
                    detail_note,
                )
                manifest.write(qr_content + "\n")
                matched_order_tags += 1
                cell_idx += NUM_COLUMNS
            else:
                student = row
                tag_number += 1
                qr_content = _render_roster_tag(
                    c,
                    width,
                    height,
                    base_name,
                    cell_idx,
                    tag_number,
                    sheet_name,
                    student,
                )
                manifest.write(qr_content + "\n")
                students_without_orders += 1
                cell_idx += 1

    if mode == "orders" and current_output_page > 0:
        _draw_orders_page_counter(c, width, height, current_output_page, total_orders_pages)

    c.save()

    unmatched_pdf_names = tuple(
        pdf_names[i]
        for i, matched_excel_idx in pdf_to_excel_idx.items()
        if matched_excel_idx is None and 0 <= i < len(pdf_names)
    )

    return QRGenerationResult(
        mode=mode,
        output_pdf_path=str(output_pdf_path),
        manifest_path=str(manifest_path),
        total_tags=tag_number,
        matched_order_tags=matched_order_tags,
        parsed_order_items=int(parsed_order_items),
        parsed_order_names=len(pdf_names),
        students_without_orders=students_without_orders,
        unmatched_pdf_names=unmatched_pdf_names,
    )
