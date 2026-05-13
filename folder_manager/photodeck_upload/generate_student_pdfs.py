#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate per-student printable PDFs from portrait folders.

Folder name format:
    "<Student Name> [<Class, Letter>]._<PASSWORD>_"  or
    "<Student Name> [<Class, Letter>]_<PASSWORD>_"

Behavior:
- USE_AI_RANKING = False -> always generate PDF (no photos in the PDF).
- USE_AI_RANKING = True  -> rank photos; if model/import fails, fall back safely; still generate PDF.
- Always ignores any file that contains "#cover" (case-insensitive) or has parentheses "()" in its filename.

Tip: Turn on DEBUG to see exactly which images were found/filtered.
"""

# -----------------------------
# USER SETTINGS
# -----------------------------
USE_AI_RANKING = True     # True -> pick best photos with model; False -> no photos in PDFs
TOPK = 4                  # Number of photos to include (max) when AI ranking is ON
DEBUG = False             # Print per-student image discovery details (set True for troubleshooting)

# -----------------------------
# IMPORTS
# -----------------------------
import os
import re
import io
import sys
import qrcode
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth

# ---- Optional AI ranking module (we'll still use our own image gatherer) ----
_rank_group = None
_AestheticScorer = None

if __package__ is None or __package__ == "":
    from pathlib import Path

    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from folder_manager.photodeck_upload.links import build_student_gallery_url, photodeck_url_path
    from folder_manager.proofing_online.passwords import extract_proofing_password, strip_proofing_password

    import_target = "create_pdf.portrait_rank_v2"
else:
    from .links import build_student_gallery_url, photodeck_url_path
    from folder_manager.proofing_online.passwords import extract_proofing_password, strip_proofing_password

    import_target = ".portrait_rank_v2"

try:
    if import_target.startswith("."):
        from .portrait_rank_v2 import rank_group as _rank_group, AestheticScorer as _AestheticScorer
    else:
        from create_pdf.portrait_rank_v2 import rank_group as _rank_group, AestheticScorer as _AestheticScorer
except Exception:
    def _rank_group(paths, scorer):
        # naive "ranking": sort by filename asc with a stable 0.0 score
        return [(p, 0.0) for p in sorted(paths)]

    class _AestheticScorer:
        def __init__(self, *args, **kwargs):
            pass

# -----------------------------
# UTILS
# -----------------------------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

def slugify(s: str) -> str:
    """Use the same path-safe text normalization as PhotoDeck gallery URLs."""
    return photodeck_url_path(s)

def _is_usable_image(path: str) -> bool:
    """Reject #cover and any file that has parentheses in its basename."""
    name = os.path.basename(path).lower()
    if "#cover" in name:
        return False
    if "(" in name or ")" in name:
        return False
    return True

def _gather_images(folder: str):
    """Robust recursive image discovery (independent of portrait_rank_v2.find_images)."""
    found = []
    for root, _, files in os.walk(folder):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXTS:
                found.append(os.path.join(root, f))
    return found

def _dbg(msg: str):
    if DEBUG:
        print(msg)

# -----------------------------
# PDF CREATION
# -----------------------------
def _draw_link_button(c, x, y, w, h, label, url, font="Helvetica-Bold", font_size=12):
    c.saveState()
    # Button background
    c.setFillColorRGB(0.12, 0.38, 0.95)  # blue
    c.roundRect(x, y, w, h, 10, stroke=0, fill=1)
    # Label
    c.setFont(font, font_size)
    c.setFillColorRGB(1, 1, 1)  # white text
    tw = c.stringWidth(label, font, font_size)
    c.drawString(x + (w - tw) / 2, y + (h - font_size) / 2 + 2, label)
    # Clickable area
    c.linkURL(url, (x, y, x + w, y + h), relative=0)
    c.restoreState()

def _draw_text_watermark(c, x, y, w, h, text, angle=30, opacity=0.18):
    """Overlay semi-transparent rotated text centered within the rect (x,y,w,h)."""
    if not text:
        return
    c.saveState()
    # Try explicit alpha first; fall back to Color alpha if needed
    try:
        c.setFillAlpha(opacity)
        c.setStrokeAlpha(opacity)
        c.setFillColor(colors.black)
    except Exception:
        # Some ReportLab builds support alpha on colors
        try:
            c.setFillColor(colors.Color(0, 0, 0, alpha=opacity))
        except Exception:
            # Last resort: light gray without transparency
            c.setFillColor(colors.grey)
    # Position + rotate
    c.translate(x + w / 2.0, y + h / 2.0)
    c.rotate(angle)
    # Pick a font size that fits ~80% of the box width
    font_name = "Helvetica-Bold"
    target_w = w * 0.80
    size = min(64, int(h * 0.35))  # start reasonably big
    while size > 8 and stringWidth(text, font_name, size) > target_w:
        size -= 2
    c.setFont(font_name, size)
    c.drawCentredString(0, 0, text)
    c.restoreState()

def draw_student_page(c, student_name, class_name, password, link_url, top_images, watermark_text="PROOF"):
    width, height = letter
    margin = 0.5 * inch

    # Title
    c.setFont("Helvetica-Bold", 36)
    c.drawCentredString(width / 2, height - 1.25 * inch, "ORDER NOW")
    c.setFont("Helvetica-Bold", 20)
    title_line = f"{student_name} - {class_name}" if class_name else student_name
    c.drawCentredString(width / 2, height - 2 * inch, title_line)

    # QR Code
    qr_img = qrcode.make(link_url)
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_reader = ImageReader(qr_buf)

    qr_size = 3.5 * inch
    qr_x = (width - qr_size) / 2
    qr_y = (height / 2) - (qr_size / 2) + 0.75 * inch
    c.drawImage(qr_reader, qr_x, qr_y, qr_size, qr_size)

    # Password + copy
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(colors.black)
    c.drawCentredString(width / 2, qr_y - 0.1 * inch, f"Password: {password}")
    c.drawCentredString(width / 2, qr_y - 0.4 * inch, "It's not too late - Free shipping to School Available for 5 days")
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, qr_y - 0.6 * inch, "Aún estás a tiempo: envío gratuito a la escuela disponible durante 5 días")

    # Button
    btn_w, btn_h = 3.25 * inch, 0.55 * inch
    btn_x = (width - btn_w) / 2
    btn_y = qr_y - 1.3 * inch
    _draw_link_button(c, btn_x, btn_y, btn_w, btn_h, "View & Order Online", link_url)

    # Optional photos + watermark
    if top_images:
        n = len(top_images)
        img_w = (width - 2 * margin - (n - 1) * 0.25 * inch) / n
        img_h = 2.5 * inch
        y_pos = margin
        for i, img_path in enumerate(top_images):
            x_pos = margin + i * (img_w + 0.25 * inch)
            try:
                with Image.open(img_path) as im:
                    im.thumbnail((img_w, img_h))
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=90)
                    buf.seek(0)
                    c.drawImage(ImageReader(buf), x_pos, y_pos, width=img_w, height=img_h,
                                preserveAspectRatio=True, anchor='sw')
                    _draw_text_watermark(c, x_pos, y_pos, img_w, img_h,
                                         watermark_text if watermark_text else f"{student_name} - PROOF")
            except Exception as e:
                print(f"[WARN] Could not embed {img_path}: {e}")

    c.showPage()  # <- advance to next page; do NOT save the canvas here


# -----------------------------
# PARSING HELPERS - tolerate messy punctuation after the class bracket
# -----------------------------

# Grab everything up to the first closing bracket as the class label, but keep the tail
# (so we can hunt for the password even if there's random spacing/punctuation before it).
BRACKET_RE = re.compile(
    r"^\s*(?P<student>.*?)\s*\[(?P<class>[^\]]+)\](?P<after>.*)$"
)

# Look for `_PASSWORD_` anywhere after the closing bracket (spaces/dots allowed before it).
PASSWORD_AFTER_CLASS_RE = re.compile(r"_(?P<pwd>[^_]+)_")

# No-bracket format: use the entire folder prefix as the child name, ignore class division
NO_CLASS_RE = re.compile(
    r"^\s*-?\s*(?P<student>.+?)\s*_(?P<pwd>[^_]+)_\s*$"
)

def normalize_class(text: str) -> str:
    """Normalize class names, ensuring no weird spaces, commas, or formatting issues."""
    t = text.strip()
    return " ".join(t.split())

def _class_key(text: str) -> str:
    base = normalize_class(text).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base)
    return re.sub(r"-+", "-", base).strip("-")

def _class_display(text: str) -> str:
    disp = normalize_class(text)
    disp = disp.rstrip(" .,_-")
    return disp or normalize_class(text)

def _clean_student(raw: str) -> str:
    """Trim stray leading punctuation/hyphens and collapse whitespace in student names."""
    raw = raw.lstrip("-_ .")
    return " ".join(raw.split())

def parse_folder_name(folder_name: str):
    folder_name = folder_name.strip()
    _dbg(f"Trying to parse folder: '{folder_name}'")

    # Bracket-first: once we see [...] treat it as the class, regardless of what punctuation
    # comes next, then pull the first `_PASSWORD_` that appears after the bracket.
    m = BRACKET_RE.match(folder_name)
    if m:
        student = _clean_student(m.group("student"))
        class_name = normalize_class(m.group("class"))
        tail = m.group("after")
        password = extract_proofing_password(folder_name)
        if not password:
            pwd_match = PASSWORD_AFTER_CLASS_RE.search(tail) or re.search(r"_(?P<pwd>[^_]+)\s*$", tail)
            password = pwd_match.group("pwd") if pwd_match else None
        if password:
            _dbg(f"Parsed (bracketed): Student = {student}, Class = {class_name}, Password = {password}")
            return student, class_name, password, True
        _dbg(f"Bracketed folder but missing password after class: '{folder_name}'")
        return student, class_name, None, True

    password = extract_proofing_password(folder_name)
    if password:
        student = _clean_student(strip_proofing_password(folder_name))
        _dbg(f"Parsed (no class): Student = {student}, Password = {password}")
        return student, None, password, False

    # No brackets: treat the whole prefix as the student name, don't group by class
    m = NO_CLASS_RE.match(folder_name)
    if m:
        student = _clean_student(m.group("student"))
        password = m.group("pwd")
        _dbg(f"Parsed (no class): Student = {student}, Password = {password}")
        return student, None, password, False
    
    _dbg(f"No match for folder: '{folder_name}'")
    return None, None, None, False


# -----------------------------
# AI RANKING WRAPPER
# -----------------------------
def rank_topk_images(img_paths, scorer, topk):
    """Rank images; tolerate different rank_group return formats."""
    if not img_paths:
        return []
    try:
        ranked = _rank_group(img_paths, scorer)
        # Handle [(path, score), ...] or [{"path":..., "score":...}, ...]
        if ranked and isinstance(ranked[0], (list, tuple)) and len(ranked[0]) >= 1:
            return [p for p, *_ in ranked[:topk]]
        if ranked and isinstance(ranked[0], dict) and "path" in ranked[0]:
            return [d["path"] for d in ranked[:topk]]
        # Unknown format — fall back to filename sort
        return sorted(img_paths)[:topk]
    except Exception as e:
        print(f"  rank_group failed ({e}). Falling back to naive selection.")
        return sorted(img_paths)[:topk]

# -----------------------------
# MAIN
# -----------------------------
def main(root_dir, gallery_name=None):
    root_dir = os.path.abspath(root_dir)

    root_gallery_name = str(gallery_name or os.path.basename(root_dir)).strip() or os.path.basename(root_dir)
    proper_link = photodeck_url_path(root_gallery_name)
    print(f"PhotoDeck root gallery for PDF links: {root_gallery_name}")
    print(f"Cleaned PhotoDeck gallery path: {proper_link}")

    pdfs_root = os.path.join(root_dir, "PDFs")
    os.makedirs(pdfs_root, exist_ok=True)

    subfolders = [f.path for f in os.scandir(root_dir) if f.is_dir()]
    subfolders = [f for f in subfolders if os.path.basename(f).lower() != "pdfs"]

    if not subfolders:
        print("No student folders found.")
        return

    print(f"\nFound {len(subfolders)} student folders.\n")

    # Init AI scorer once (same as your current logic)
    scorer = None
    if USE_AI_RANKING:
        try:
            print("Loading improved-v2-vit-l14 on cuda...")
            scorer = _AestheticScorer("improved-v2-vit-l14", device="cuda")
            print("AI ranking: CUDA")
        except Exception as e:
            print(f"AI ranking: CUDA unavailable ({e}), trying CPU...")
            try:
                scorer = _AestheticScorer("improved-v2-vit-l14", device="cpu")
                print("AI ranking: CPU")
            except Exception as e2:
                print(f"AI ranking disabled — model init failed ({e2}). Using naive selection.")
                scorer = None

    # --- 1) Parse all folders and bucket by class (or collect solo) ---
    by_class = {}
    solo_items = []

    for folder in sorted(subfolders):
        fname = os.path.basename(folder)
        student_name, class_name, password, has_class = parse_folder_name(fname)
        if not student_name or not password:
            print(f"[SKIP] Unrecognized or incomplete: {fname}")
            continue

        if has_class and class_name:
            canon = _class_key(class_name)
            display_name = _class_display(class_name)
            bucket = by_class.setdefault(canon, {"display": display_name, "items": []})
            if display_name and len(display_name) > len(bucket["display"]):
                bucket["display"] = display_name
            bucket["items"].append((folder, student_name, password))
        else:
            solo_items.append((folder, student_name, password))

    if not by_class and not solo_items:
        print("No parsable student folders.")
        return

    class_buckets = sorted(by_class.values(), key=lambda data: data["display"].lower()) if by_class else []

    total_students = sum(len(data["items"]) for data in class_buckets) + len(solo_items)
    if total_students == 0:
        print("No students found after parsing.")
        return

    # --- 2) For each class, open ONE canvas and render a page per student ---
    for class_data in class_buckets:
        class_name = class_data["display"]
        items = class_data["items"]
        class_pdf_dir = os.path.join(pdfs_root, f"{class_name} PDFs")
        os.makedirs(class_pdf_dir, exist_ok=True)

        # Single output per class:
        class_count = len(items)
        ratio_suffix = f"{class_count} of {total_students}"
        pdf_filename = f"{class_name} {ratio_suffix}.pdf"
        class_pdf_path = os.path.join(class_pdf_dir, pdf_filename)
        c = canvas.Canvas(class_pdf_path, pagesize=letter)

        print(f"\n== Class: {class_name} — {class_count} students ({ratio_suffix}) ==")

        # Keep pages in student-name order
        for folder, student_name, password in sorted(items, key=lambda t: t[1].lower()):
            print(f"  - {student_name}")

            link_url = build_student_gallery_url(root_gallery_name, student_name, class_name)

            # Find/top-k images as before
            raw = _gather_images(folder)
            all_images = [p for p in raw if _is_usable_image(p)]
            if USE_AI_RANKING and scorer is not None:
                top_imgs = rank_topk_images(all_images, scorer, TOPK) if all_images else []
            else:
                top_imgs = []

            # Render ONE PAGE for this student into the shared class canvas
            draw_student_page(
                c,
                student_name,
                class_name,
                password,
                link_url,
                top_imgs,
                watermark_text="SCHOOLPHOTOSNYC - PROOF"
            )

        # Save the one-per-class PDF
        c.save()
        print(f"  ✓ Class PDF created: {class_pdf_path}")

    # --- 3) Produce one PDF per child when no class brackets were found ---
    if solo_items:
        solo_dir = os.path.join(pdfs_root, "Individual PDFs")
        os.makedirs(solo_dir, exist_ok=True)
        print(f"\n== Individual PDFs (no class brackets) — {len(solo_items)} students ==\n")
        for folder, student_name, password in sorted(solo_items, key=lambda t: t[1].lower()):
            print(f"  - {student_name}")
            slug_name = slugify(student_name) or "student"
            link_url = build_student_gallery_url(root_gallery_name, student_name, None)

            raw = _gather_images(folder)
            all_images = [p for p in raw if _is_usable_image(p)]
            if USE_AI_RANKING and scorer is not None:
                top_imgs = rank_topk_images(all_images, scorer, TOPK) if all_images else []
            else:
                top_imgs = []

            pdf_filename = f"{slug_name}_{password}.pdf"
            pdf_path = os.path.join(solo_dir, pdf_filename)
            c = canvas.Canvas(pdf_path, pagesize=letter)
            draw_student_page(
                c,
                student_name,
                None,
                password,
                link_url,
                top_imgs,
                watermark_text="SCHOOLPHOTOSNYC - PROOF"
            )
            c.save()
            print(f"    ✓ PDF created: {pdf_path}")

    print("\nAll done! PDFs saved under:", pdfs_root)


# -----------------------------
# ENTRY POINT
# -----------------------------
if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog
    if len(sys.argv) >= 2:
        root_folder = sys.argv[1]
    else:
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        root_folder = filedialog.askdirectory(title="Select Main Folder with Student Subfolders", parent=root)
        root.destroy()
        if not root_folder:
            print("No folder selected. Exiting.")
            sys.exit(0)
    main(root_folder)
