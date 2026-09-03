import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

import common
import requests
from pdf2image import convert_from_bytes
from PIL import Image

try:
    import fitz
except ImportError:
    fitz = None

try:
    from supabase import create_client
except ImportError:
    create_client = None

# Supabase Storage is optional (used only with --storage supabase).
# Set SUPABASE_URL and SUPABASE_KEY in the environment when you need it;
# the default local mode needs neither.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

BASE_URL = "https://ibdocs.re/p/IB PAST PAPERS - YEAR/"
ROOT_MARKER = "IB PAST PAPERS - YEAR"
BUCKET_NAME = "past-papers"
STORAGE_PREFIX = "agent-uploads"

SLICES_PER_PAGE = 2
SPLIT_MODE = "vertical"
SLICE_MODE = "questions"
PDF_DPI = 150
IMAGE_FORMAT = "JPEG"
JPEG_QUALITY = 85
IMAGE_EXT = ".jpg" if IMAGE_FORMAT == "JPEG" else ".png"
EXCLUDE_TRANSLATIONS = True
TRANSLATION_WORDS = ("spanish", "french", "german")
REQUEST_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT = 180
MS_SUFFIXES = ("_markscheme", "_mark_scheme", "_ms")
CLOUDFLARE_HINT = b"Just a moment"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("extract")

DEFAULT_LIST_FILE = str(common.MASTER_LIST)

FILENAME_RE = re.compile(r"([^\s\\/]+\.pdf)\s*$", re.IGNORECASE)
LEVEL_RE = re.compile(r"_(HL|SL|HLSL)(?=_|\.|$)")
PAPER_RE = re.compile(r"_paper_(\d+)")
DIR_DUMP_HEADER = "Directory of "
DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass
class Entry:
    filename: str
    rel_dir: tuple
    year: int
    url: str
    subject: str
    is_markscheme: bool

    @property
    def stem(self):
        return self.filename[:-4]

    @property
    def session(self):
        for part in self.rel_dir:
            match = re.match(r"^(May|November)\s+\d{4}", part)
            if match:
                return match.group(1)
        return None


def collapse(s: str) -> str:
    return re.sub(r"_+", "_", s)


def level_of(stem: str):
    match = LEVEL_RE.search(stem)
    return match.group(1) if match else None


def is_translation(stem: str) -> bool:
    low = stem.lower()
    boundary = r"(?:^|[^a-z0-9])"
    return any(
        re.search(boundary + word + r"(?:$|[^a-z0-9])", low)
        for word in TRANSLATION_WORDS
    )


def classify_subject(filename: str):
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    if EXCLUDE_TRANSLATIONS and is_translation(stem):
        return None
    low = stem.lower()
    if "aahl" in low:
        return "Mathematics_AAHL"
    if "aasl" in low:
        return None
    if low.startswith("math ") or " math " in low:
        if re.search(r"\baa\b", low):
            return "Mathematics_AAHL" if " hl" in low else None
        return None
    level = level_of(stem)
    if level is None:
        return None
    if stem.startswith("Mathematics_analysis_and_approaches"):
        return "Mathematics_AAHL" if level == "HL" else None
    if "applications_and_interpretation" in stem:
        return None
    if stem.startswith(("Mathematics", "Mathematical", "Further_mathematics")):
        return "Mathematics_HL" if stem.startswith("Mathematics") and level == "HL" else None
    if stem.startswith("Economics"):
        return "Economics_HL" if level == "HL" else None
    if stem.startswith("Physics"):
        return "Physics_SL" if level == "SL" else None
    if stem.startswith("Computer_science"):
        return "Computer_Science"
    return None


def is_markscheme(stem: str) -> bool:
    return any(stem.endswith(suffix) for suffix in MS_SUFFIXES)


def rel_parts_from_drive_path(path: str):
    idx = path.find(ROOT_MARKER)
    if idx == -1:
        return None
    return path[idx + len(ROOT_MARKER):].strip("\\/").split("\\")


def extract_year(rel_dir: tuple, filename: str):
    for part in rel_dir:
        match = re.match(r"^(\d{4}) Examination Session$", part)
        if match:
            return int(match.group(1))
    for part in rel_dir:
        match = re.search(r"(19|20)\d{2}", part)
        if match:
            return int(match.group(0))
    match = re.search(r"(?:^|[_\s])((?:19|20)\d{2})(?:[_\s]|$)", filename)
    if match:
        return int(match.group(1))
    return None


def build_url(rel_dir: tuple, filename: str) -> str:
    parts = [BASE_URL.rstrip("/")]
    parts.extend(quote(part, safe="") for part in rel_dir)
    parts.append(quote(filename, safe=""))
    return "/".join(parts)


def parse_list_file(path: Path):
    entries = []
    current_rel = None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        if DIR_DUMP_HEADER in line:
            drive_path = line.split(DIR_DUMP_HEADER, 1)[1].strip()
            current_rel = rel_parts_from_drive_path(drive_path)
            continue

        if line.lower().startswith("http"):
            url_path = unquote(line)
            idx = url_path.find(ROOT_MARKER)
            if idx == -1:
                continue
            filename = url_path.rstrip("/").split("/")[-1]
            if not filename.lower().endswith(".pdf"):
                continue
            parts = url_path[idx + len(ROOT_MARKER):].strip("/").split("/")
            rel_dir = tuple(parts[:-1])
            filename = parts[-1]
        elif DRIVE_PATH_RE.match(line):
            rel = rel_parts_from_drive_path(line)
            if rel is None or not line.lower().endswith(".pdf"):
                continue
            rel_dir = tuple(rel[:-1])
            filename = rel[-1]
        else:
            match = FILENAME_RE.search(line)
            if not match:
                continue
            filename = match.group(1)
            if current_rel is None:
                rel_dir = ()
            else:
                rel_dir = tuple(current_rel)

        year = extract_year(rel_dir, filename)
        if year is None:
            log.warning("Skipping %s: could not determine year", filename)
            continue

        subject = classify_subject(filename)
        if subject is None:
            continue

        entries.append(
            Entry(
                filename=filename,
                rel_dir=rel_dir,
                year=year,
                url=build_url(rel_dir, filename),
                subject=subject,
                is_markscheme=is_markscheme(filename[:-4]),
            )
        )

    unique = {}
    for entry in entries:
        unique.setdefault((entry.rel_dir, entry.filename), entry)
    return list(unique.values())


def pair_question_papers(entries):
    qps_by_dir = defaultdict(list)
    ms_by_dir = defaultdict(dict)

    for entry in entries:
        if entry.is_markscheme:
            ms_by_dir[entry.rel_dir][collapse(entry.stem)] = entry
        else:
            qps_by_dir[entry.rel_dir].append(entry)

    pairs = []
    for rel_dir, qps in qps_by_dir.items():
        for qp in qps:
            key = collapse(qp.stem)
            ms_entry = None
            for suffix in MS_SUFFIXES:
                ms_entry = ms_by_dir.get(rel_dir, {}).get(key + suffix)
                if ms_entry:
                    break
            pairs.append((qp, ms_entry))

    pairs.sort(key=lambda pair: (pair[0].year, pair[0].filename))
    return pairs


def build_session(headers=None, cookies=None):
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})
    if headers:
        session.headers.update(headers)
    if cookies:
        session.cookies.update(cookies)
    return session


def download_pdf(session, url):
    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            content = response.content
            if response.status_code == 200 and content[:5] == b"%PDF-":
                return content
            if response.status_code in (403, 429) and CLOUDFLARE_HINT in content[:2000]:
                raise RuntimeError(
                    "Cloudflare challenge page returned. Open the site in a browser, "
                    "then provide the cf_clearance cookie via --cookies."
                )
            last_error = RuntimeError(
                f"GET {url} returned HTTP {response.status_code}"
            )
        except requests.RequestException as error:
            last_error = error
        log.warning(
            "Attempt %d/%d failed for %s: %s",
            attempt, REQUEST_RETRIES, url, last_error,
        )
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_error


def pages_to_images(pdf_bytes):
    try:
        return convert_from_bytes(pdf_bytes, dpi=PDF_DPI)
    except Exception as error:
        raise RuntimeError(
            "pdf2image failed (is poppler installed and on PATH?): "
            f"{error}"
        ) from error


def slice_page(image, index):
    width, height = image.size
    if SPLIT_MODE == "vertical":
        top = height * index // SLICES_PER_PAGE
        bottom = height * (index + 1) // SLICES_PER_PAGE
        return image.crop((0, top, width, bottom))
    left = width * index // SLICES_PER_PAGE
    right = width * (index + 1) // SLICES_PER_PAGE
    return image.crop((left, 0, right, height))


MAXIMUM_MARK_RE = re.compile(r"Maximum\s+mark", re.IGNORECASE)
MARKS_VALUE_RE = re.compile(r"Maximum\s+mark\s*:?\s*[\[(]?\s*(\d{1,3})", re.IGNORECASE)
NUMBER_TOKEN_RE = re.compile(r"([A-Z]?\d{1,2})\.?\s*$")
QUESTION_PREFIX_RE = re.compile(r"Question$", re.IGNORECASE)
PREFIX_NUM_RE = re.compile(r"(\d{1,2}):?\s*$")
SECTION_RE = re.compile(r"Section\s+[A-B]\s*$", re.IGNORECASE)


def natural_question_key(question_string):
    match = re.match(r"^([A-Z])?(\d{1,2})$", str(question_string))
    if match:
        return (match.group(1) or "", int(match.group(2)))
    return (str(question_string), 0)


def paper_number_of(stem):
    match = re.search(r"paper[_\s]*(\d+)", stem, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\bP(\d{1,2})\b", stem)
    if match:
        return int(match.group(1))
    return None


def page_lines(page):
    words = page.get_text("words")
    if not words:
        return []
    lines = defaultdict(list)
    for x0, y0, x1, y1, text, block, line_no, word_no in words:
        lines[(block, line_no)].append((x0, y0, x1, y1, text))
    line_list = []
    for key, ws in lines.items():
        ws.sort(key=lambda w: w[0])
        line_list.append(ws)
    line_list.sort(key=lambda ws: min(w[1] for w in ws))
    return line_list


def detect_question_tops(page):
    line_list = page_lines(page)
    tops = []
    prev_ws = None
    for ws in line_list:
        joined = " ".join(w[4] for w in ws)
        if MAXIMUM_MARK_RE.search(joined):
            marks_match = MARKS_VALUE_RE.search(joined)
            max_marks = int(marks_match.group(1)) if marks_match else None
            for candidate in (ws, prev_ws):
                if candidate is None:
                    continue
                found = None
                for w in candidate:
                    match = NUMBER_TOKEN_RE.fullmatch(w[4])
                    if match:
                        found = (match.group(1), w[0], w[1])
                        break
                if found is not None:
                    number, x0, y0 = found
                    tops.append((number, x0, y0, max_marks))
                    break
        prev_ws = ws
    tops.sort(key=lambda top: (top[2], top[1]))
    return tops


def safe_rect(x0, y0, x1, y1, min_width=10, min_height=10):
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 < min_width:
        x1 = x0 + min_width
    if y1 - y0 < min_height:
        y1 = y0 + min_height
    return fitz.Rect(x0, y0, x1, y1)


def question_regions(page, tops, header_pad=8, bottom_margin=30, column_gap=40):
    columns = []
    for number, x0, y0, max_marks in tops:
        for column in columns:
            if abs(column["x"] - x0) < column_gap:
                column["items"].append((number, y0, max_marks))
                break
        else:
            columns.append({"x": x0, "items": [(number, y0, max_marks)]})
    for column in columns:
        column["items"].sort(key=lambda item: item[1])
    columns.sort(key=lambda column: column["x"])

    x_positions = [column["x"] for column in columns]
    page_height = page.rect.height
    regions = []
    for column_index, column in enumerate(columns):
        left = column["x"] - 6
        if column_index + 1 < len(x_positions):
            right = x_positions[column_index + 1] - 10
        else:
            right = page.rect.width - 30
        items = column["items"]
        for i, (number, y0, max_marks) in enumerate(items):
            top = max(0, y0 - header_pad)
            if i + 1 < len(items):
                bottom = items[i + 1][1] - 4
            else:
                bottom = page_height - bottom_margin
            regions.append(
                (number, safe_rect(left, top, right, bottom), max_marks)
            )
    regions.sort(key=lambda region: (region[1].y0, region[1].x0))
    return regions


def crop_region(image, rect, scale):
    box = (
        max(0, int(rect.x0 * scale)),
        max(0, int(rect.y0 * scale)),
        min(image.width, int(rect.x1 * scale)),
        min(image.height, int(rect.y1 * scale)),
    )
    return image.crop(box)


def region_text(page, rect):
    text = page.get_text("text", clip=rect)
    text = re.sub(r"(.)\1{19,}", "", text)
    text = re.sub(r"(?:Turn over\s*)+", " ", text)
    text = re.sub(r"References:.*$", "", text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", text).strip()


CONTINUED_PAREN_RE = re.compile(
    r"\(\s*[^)]*question\s+(\d{1,2})\s*[:.]?\s*continued\s*\)", re.IGNORECASE
)
CONTINUED_PLAIN_RE = re.compile(
    r"\bQuestion\s+(\d{1,2})\s*[:.]?\s*continued\b", re.IGNORECASE
)
OPTION_HEADER_RE = re.compile(r"^Option\s+[A-Z]", re.IGNORECASE)


def detect_continuation(page):
    for ws in page_lines(page):
        joined = " ".join(w[4] for w in ws)
        match = CONTINUED_PAREN_RE.search(joined)
        if not match:
            match = CONTINUED_PLAIN_RE.search(joined)
        if match:
            return int(match.group(1)), min(w[1] for w in ws)
    return None


def split_question_token(token):
    match = re.match(r"^([A-Z])(\d{1,2})$", token)
    if match:
        return match.group(1), int(match.group(2))
    return None, int(token)


def analyze_doc_sequence(doc, margin_limit=55, lettered_limit=75,
                         require_period=False, reject_long_lines=False,
                         restart_reset=False):
    pages_out = []
    last_letter = None
    last_number = None
    late_tops_seen = False
    for page in doc:
        line_list = page_lines(page)
        if page.number <= 2 and is_front_matter_index(line_list, lettered_limit):
            # Markscheme reading-instructions / contents page: a dense run of
            # lone "N." lines. Accepting it pre-empts the question sequence
            # and truncates every real question to a one-line sliver.
            pages_out.append(([], detect_continuation(page)))
            continue
        tops = []
        prev_joined = ""
        for ws in line_list:
            first = ws[0]
            joined = " ".join(w[4] for w in ws).strip()
            letter = None
            number = None
            x0 = first[0]
            match = NUMBER_TOKEN_RE.fullmatch(first[4])
            if match:
                cand_letter, cand_number = split_question_token(match.group(1))
                limit = lettered_limit if cand_letter else margin_limit
                if require_period and not first[4].rstrip().endswith("."):
                    pass  # bare "N" counters (instructions), not headers
                elif reject_long_lines and len(ws) > 6:
                    pass  # prose sentence (examiner notes), not a header
                elif x0 < limit:
                    letter, number = cand_letter, cand_number
            elif (
                len(ws) >= 2
                and QUESTION_PREFIX_RE.fullmatch(first[4])
                and "continued" not in joined.lower()
                and first[0] < 100
            ):
                match = PREFIX_NUM_RE.fullmatch(ws[1][4])
                if match:
                    number = int(match.group(1))
            if number is None:
                prev_joined = joined
                continue
            if (
                restart_reset
                and number == 1
                and letter == last_letter
                and last_number is not None
                and page.number > 2
                and not late_tops_seen
                and not OPTION_HEADER_RE.match(prev_joined)
            ):
                # The accepted "questions" so far were all on the first three
                # pages (front-matter junk); this is the real question 1.
                for i in range(min(3, len(pages_out))):
                    pages_out[i] = ([], pages_out[i][1])
                last_letter = None
                last_number = None
            if last_number is None:
                accepted = number == 1
            elif letter == last_letter:
                accepted = (
                    number == last_number + 1
                    or (
                        last_number < number <= last_number + 3
                        and len(ws) <= 2
                    )
                    or (number == 1 and OPTION_HEADER_RE.match(prev_joined))
                )
            else:
                accepted = number == 1
            if accepted:
                label = f"{letter}{number}" if letter else str(number)
                tops.append((label, x0, first[1], None))
                last_letter, last_number = letter, number
                if page.number > 2:
                    late_tops_seen = True
            prev_joined = joined
        pages_out.append((tops, detect_continuation(page)))
    return pages_out


def is_front_matter_index(line_list, margin=75):
    """True for pages holding a dense run of >=6 lone "N." lines numbered
    1..N (markscheme reading instructions or a contents list)."""
    lone = []
    for ws in line_list:
        if len(ws) != 1:
            continue
        match = NUMBER_TOKEN_RE.fullmatch(ws[0][4])
        if match and ws[0][0] < margin:
            lone.append(split_question_token(match.group(1))[1])
    return len(lone) >= 6 and lone == list(range(1, len(lone) + 1))


def analyze_doc(doc):
    pages = []
    for page in doc:
        pages.append((detect_question_tops(page), detect_continuation(page)))
    return pages


def _ms_section_pass(doc, bare_limit=60, lettered_limit=75):
    """One section-gated detection pass over the markscheme.

    Bare question headers normally sit at x0 < 60, but some markschemes
    indent them a couple of points deeper (e.g. x0=61). Lettered headers
    (A1., B2.) sit deeper still (x0~66). Junk number tokens (marks, list
    items) also live beyond x0=60, so the wider bare limit is only used as
    a rescue pass when the strict pass finds nothing at all.
    """
    pages_out = []
    last_letter = None
    last_number = None
    seen_section = False
    prev_joined = ""
    for page in doc:
        tops = []
        line_list = page_lines(page)
        for ws in line_list:
            joined = " ".join(w[4] for w in ws).strip()
            if SECTION_RE.match(joined):
                seen_section = True
            if not seen_section:
                prev_joined = joined
                continue
            first = ws[0]
            match = NUMBER_TOKEN_RE.fullmatch(first[4])
            if not match:
                prev_joined = joined
                continue
            letter, number = split_question_token(match.group(1))
            limit = lettered_limit if letter else bare_limit
            if first[0] >= limit:
                prev_joined = joined
                continue
            accepted = False
            if last_number is None:
                accepted = number == 1
            elif letter == last_letter:
                if number == last_number + 1:
                    accepted = True
                elif last_number + 1 < number <= last_number + 3 and len(ws) <= 2:
                    accepted = True
                    log.warning(
                        "Markscheme page %d: expected question %d, found %d",
                        page.number, last_number + 1, number,
                    )
                elif number == 1 and OPTION_HEADER_RE.match(prev_joined):
                    accepted = True
            elif number == 1:
                accepted = True
            if accepted:
                label = f"{letter}{number}" if letter else str(number)
                tops.append((label, first[0], first[1], None))
                last_letter, last_number = letter, number
            prev_joined = joined
        pages_out.append((tops, detect_continuation(page)))
    return pages_out, seen_section


def analyze_markscheme_doc(doc, return_orphans=False):
    pages_out, seen_section = _ms_section_pass(doc)
    if seen_section and not any(tops for tops, cont in pages_out):
        # Strict margin found nothing: retry with the wider bare margin.
        pages_out, seen_section = _ms_section_pass(doc, bare_limit=75)
    if not seen_section:
        log.warning("Markscheme has no Section marker; using sequence detection")
        # Tier 1: headers with periods ("1.", "A1.") at x0<60. Markscheme
        # front matter uses bare digits ("1", "4") for instruction counters
        # and long prose lines for numbered examiner notes — both filtered.
        pages_out = analyze_doc_sequence(
            doc, margin_limit=60, require_period=True, reject_long_lines=True,
            restart_reset=True,
        )
        if not any(tops for tops, cont in pages_out):
            # Tier 2: 2025-era Physics markschemes use bare digit headers
            # ("1" at x0=51); their instruction items carry periods and sit
            # deeper (x0~61), so the roles are reversed there.
            pages_out = analyze_doc_sequence(
                doc, margin_limit=55, reject_long_lines=True,
                restart_reset=True,
            )
        tops_pages = sum(1 for tops, cont in pages_out if tops)
        first_top_page = next(
            (i for i, (tops, cont) in enumerate(pages_out) if tops), None
        )
        if (
            first_top_page is not None
            and len(doc) >= 8
            and all(
                not tops
                for tops, cont in pages_out[first_top_page + 3:]
            )
        ):
            # Everything was "detected" on the first couple of pages with a
            # long headerless tail: those are examiner notes, not questions.
            log.warning(
                "Markscheme detection collapsed to front matter; no usable "
                "question headers"
            )
            pages_out = [([], cont) for tops, cont in pages_out]
    attached, orphan_count = attach_orphan_ms_pages(pages_out)
    if return_orphans:
        return attached, orphan_count
    return attached


def attach_orphan_ms_pages(pages_out, orphan_top=60.0):
    """Attach markscheme pages with no question header to the current question.

    Markscheme answers often span several pages, but (unlike question papers)
    the follow-on pages usually have no "Question N continued" marker. Without
    this, every page after an answer's first page would be dropped.

    Returns (pages_out, orphan_count): orphan_count is the number of content
    pages that had no header and no continuation marker after the first
    question — i.e. pages older outputs silently dropped.
    """
    attached = []
    last_label = None
    orphan_count = 0
    for tops, cont in pages_out:
        if tops:
            last_label = tops[-1][0]
        elif cont is not None:
            last_label = str(cont[0])
        elif last_label is not None:
            cont = (last_label, orphan_top)
            orphan_count += 1
        attached.append((tops, cont))
    return attached, orphan_count


def page_span_left(page, y_top, y_bottom):
    x_values = [
        x0
        for x0, y0, x1, y1, text, block, line_no, word_no in page.get_text("words")
        if y_top <= y0 <= y_bottom
    ]
    if not x_values:
        return 37.0
    return max(0, min(x_values) - 6)


def stack_images(segment_images):
    width = max(image.width for image in segment_images)
    total_height = sum(image.height for image in segment_images)
    canvas = Image.new("RGB", (width, total_height), "white")
    offset = 0
    for image in segment_images:
        canvas.paste(image, (0, offset))
        offset += image.height
    return canvas


def should_use_full_ms(ms_records, qp_label_count):
    """Full-stack fallback: the markscheme cannot be sliced per question at
    all (answer grids, scrambled text layers, scanned PDFs)."""
    return not ms_records


def full_ms_record(qp, ms_page_images, session_tag):
    """One image stacking every markscheme page; linked from all questions."""
    images = ms_page_images
    total_height = sum(image.height for image in images)
    if total_height > 55000:  # JPEG dimension limit is 65500
        factor = 55000 / total_height
        images = [
            image.resize((int(image.width * factor),
                          int(image.height * factor)))
            for image in images
        ]
    return {
        "upload_name": f"{qp.stem}_{session_tag}_full_ms{IMAGE_EXT}",
        "image": stack_images(images),
        "question_number": "ALL",
        "raw_text": None,
        "page_number": 1,
        "max_marks": None,
    }


def plan_question_segments(
    doc, images, analysis, stem, session_tag, name_suffix=""
):
    scale = PDF_DPI / 72.0
    segments = defaultdict(list)
    for page_index, (tops, cont) in enumerate(analysis):
        page = doc[page_index]
        if not tops and cont is None:
            continue
        if tops:
            regions = question_regions(page, tops)
            first_top_y = min(rect.y0 for _, rect, _ in regions)
            if cont is not None:
                cont_number, cont_y = cont
                if cont_y < first_top_y:
                    rect = safe_rect(
                        page_span_left(page, cont_y - 20, first_top_y),
                        max(0, cont_y - 8),
                        page.rect.width - 30,
                        first_top_y - 4,
                    )
                    segments[str(cont_number)].append((page_index, rect, None))
            for number, rect, max_marks in regions:
                segments[number].append((page_index, rect, max_marks))
        else:
            cont_number, cont_y = cont
            rect = safe_rect(
                page_span_left(page, cont_y - 20, page.rect.height),
                max(0, cont_y - 8),
                page.rect.width - 30,
                page.rect.height - 30,
            )
            segments[str(cont_number)].append((page_index, rect, None))

    records = []
    for number in sorted(segments, key=natural_question_key):
        segment_images = []
        texts = []
        start_page = None
        max_marks = None
        for page_index, rect, segment_marks in segments[number]:
            cropped = crop_region(images[page_index], rect, scale)
            if cropped.width < 20 or cropped.height < 20:
                continue
            segment_images.append(cropped)
            texts.append(region_text(doc[page_index], rect))
            if start_page is None:
                start_page = page_index + 1
            if max_marks is None and segment_marks is not None:
                max_marks = segment_marks
        if not segment_images:
            continue
        records.append(
            {
                "upload_name": (
                    f"{stem}_{session_tag}_p{start_page:02d}_q{number}"
                    f"{name_suffix}{IMAGE_EXT}"
                ),
                "image": stack_images(segment_images),
                "question_number": str(number),
                "raw_text": " ".join(text for text in texts if text),
                "page_number": start_page,
                "max_marks": max_marks,
            }
        )
    return records


def halves_records(qp, images, session_tag):
    records = []
    for page_number, image in enumerate(images, start=1):
        for slice_index in range(SLICES_PER_PAGE):
            records.append(
                {
                    "upload_name": (
                        f"{qp.stem}_{session_tag}_p{page_number:02d}_"
                        f"s{slice_index + 1}{IMAGE_EXT}"
                    ),
                    "image": slice_page(image, slice_index),
                    "question_number": f"{page_number}.{slice_index + 1}",
                    "raw_text": None,
                    "page_number": page_number,
                }
            )
    return records


def encode_image(image):
    buffer = io.BytesIO()
    if IMAGE_FORMAT == "JPEG":
        image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def parse_cookies_arg(cookie_string):
    cookies = {}
    if not cookie_string:
        return cookies
    for pair in cookie_string.split(";"):
        if "=" in pair:
            name, value = pair.strip().split("=", 1)
            cookies[name] = value
    return cookies


def parse_headers_arg(header_string):
    headers = {}
    if not header_string:
        return headers
    for item in header_string.split(";"):
        if ":" in item:
            name, value = item.strip().split(":", 1)
            headers[name.strip()] = value.strip()
    return headers


def storage_public_url(storage_path):
    return (
        f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/"
        f"{BUCKET_NAME}/{quote(storage_path, safe='/')}"
    )


def upload_image(storage_path, data, content_type):
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/"
        f"{BUCKET_NAME}/{quote(storage_path, safe='/')}"
    )
    return requests.post(
        endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": content_type,
            # Reprocessed papers reuse the same object names; overwrite the
            # old (partial) images instead of keeping the stale first upload.
            "x-upsert": "true",
        },
        timeout=REQUEST_TIMEOUT,
    )


def run(
    list_file,
    limit=None,
    cookies=None,
    headers=None,
    local_dir=None,
    csv_out=None,
    subject=None,
    storage="local",
    manifest_file=None,
):
    if storage == "supabase":
        if csv_out is None:
            sys.exit("--storage supabase requires --csv-out")
        if not SUPABASE_URL or not SUPABASE_KEY:
            sys.exit(
                "--storage supabase requires SUPABASE_URL and SUPABASE_KEY "
                "environment variables"
            )
        local_dir = None
        supabase = None
    elif csv_out is not None:
        if local_dir is None:
            local_dir = common.ROOT / "extracted_samples"
        supabase = None
    else:
        if create_client is None:
            sys.exit("Missing dependency. Install with: pip install supabase")
        try:
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as error:
            sys.exit(
                f"Supabase client rejected SUPABASE_KEY: {error}. The DB-insert "
                "mode needs a legacy JWT key (anon/service_role)."
            )
    manifest_rows = {}
    done_papers = set()
    if storage == "supabase" and manifest_file is not None and os.path.exists(
        manifest_file
    ):
        with open(manifest_file, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("kind") == "paper_done":
                    done_papers.add(tuple(row["paper_key"]))
                    continue
                manifest_rows[row["storage_path"]] = row
    session = build_session(headers=headers, cookies=cookies)

    entries = parse_list_file(list_file)
    pairs = pair_question_papers(entries)
    seen = set()
    deduped = []
    for qp, ms_entry in pairs:
        key = (qp.subject, qp.year, qp.session, collapse(qp.stem))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((qp, ms_entry))
    pairs = deduped
    if subject is not None:
        pairs = [pair for pair in pairs if pair[0].subject == subject]
    log.info("Parsed %d target entries, %d question papers", len(entries), len(pairs))

    if limit is not None:
        pairs = pairs[:limit]

    stats = {"processed": 0, "uploaded": 0, "failed": 0, "missing_ms": 0, "skipped_pages": 0}
    rows = []

    for qp, ms_entry in pairs:
        paper_key = (qp.subject, qp.year, qp.session, collapse(qp.stem))
        if paper_key in done_papers:
            log.info("Skipping %s (marked done in manifest)", qp.filename)
            continue
        stats["processed"] += 1
        fails_before = stats["failed"]
        ms_url = ms_entry.url if ms_entry else None
        if ms_entry is None:
            stats["missing_ms"] += 1
            log.warning("No markscheme found for %s", qp.filename)

        try:
            pdf_bytes = download_pdf(session, qp.url)
            images = pages_to_images(pdf_bytes)
        except Exception as error:
            stats["failed"] += 1
            log.error("Download/convert failed for %s: %s", qp.filename, error)
            continue

        question_mode = SLICE_MODE == "questions" and fitz is not None
        doc = None
        if question_mode:
            try:
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            except Exception as error:
                log.warning("PyMuPDF failed to open %s: %s", qp.filename, error)
                doc = None
        if doc is not None and not any(
            page.get_text("words") for page in doc
        ):
            log.warning("No text layer in %s; falling back to halves", qp.filename)
            doc = None

        session_tag = f"{qp.session}{qp.year}" if qp.session else str(qp.year)
        paper_number = paper_number_of(qp.stem)
        if doc is not None and "case_study" in qp.filename.lower():
            # Case study booklets contain no exam questions; never slice them
            # (their numbered reference lists look like question sequences).
            log.info("Case study booklet (no questions): %s", qp.filename)
            records = []
        elif doc is not None:
            analysis = analyze_doc(doc)
            if not any(tops for tops, cont in analysis):
                log.info(
                    "No [Maximum mark] headers in %s; using sequence detection",
                    qp.filename,
                )
                analysis = analyze_doc_sequence(doc)
            stats["skipped_pages"] += sum(
                1 for tops, cont in analysis if not tops and cont is None
            )
            try:
                records = plan_question_segments(
                    doc, images, analysis, qp.stem, session_tag
                )
            except Exception as error:
                log.warning(
                    "Question slicing failed for %s: %s; using halves",
                    qp.filename, error,
                )
                records = halves_records(qp, images, session_tag)
            if not records:
                log.warning(
                    "No questions detected in %s; using halves", qp.filename
                )
                records = halves_records(qp, images, session_tag)
        else:
            records = halves_records(qp, images, session_tag)

        ms_images = {}
        if ms_entry is not None and (storage == "supabase" or local_dir):
            try:
                ms_pdf_bytes = download_pdf(session, ms_entry.url)
                ms_page_images = pages_to_images(ms_pdf_bytes)
            except Exception as error:
                ms_page_images = None
                stats["failed"] += 1  # withhold paper_done marker so a rerun retries
                log.error(
                    "Markscheme download/convert failed for %s: %s",
                    ms_entry.filename, error,
                )
            ms_doc = None
            if ms_page_images is not None and fitz is not None:
                try:
                    ms_doc = fitz.open(stream=ms_pdf_bytes, filetype="pdf")
                except Exception as error:
                    log.warning(
                        "PyMuPDF failed to open markscheme %s: %s",
                        ms_entry.filename, error,
                    )
            ms_records = []
            if ms_doc is not None and any(
                page.get_text("words") for page in ms_doc
            ):
                ms_analysis = analyze_markscheme_doc(ms_doc)
                stats["skipped_pages"] += sum(
                    1 for tops, cont in ms_analysis if not tops and cont is None
                )
                try:
                    ms_records = plan_question_segments(
                        ms_doc,
                        ms_page_images,
                        ms_analysis,
                        qp.stem,
                        session_tag,
                        name_suffix="_ms",
                    )
                except Exception as error:
                    stats["failed"] += 1  # withhold paper_done marker so a rerun retries
                    log.warning(
                        "Markscheme slicing failed for %s: %s; skipping "
                        "markscheme images",
                        ms_entry.filename, error,
                    )
                    ms_records = []
            elif ms_page_images is not None:
                log.warning(
                    "No markscheme text layer for %s",
                    ms_entry.filename,
                )
            qp_labels = {r["question_number"] for r in records}
            ms_labels = set()
            if ms_page_images is not None:
                if should_use_full_ms(ms_records, len(qp_labels)):
                    ms_records = [full_ms_record(qp, ms_page_images, session_tag)]
                else:
                    ms_labels = {r["question_number"] for r in ms_records}
                    if qp_labels - ms_labels:
                        # Questions the MS slicing missed still get the
                        # whole-markscheme stack as their markscheme image.
                        gap_record = full_ms_record(
                            qp, ms_page_images, session_tag
                        )
                        gap_record["question_number"] = "GAP"
                        ms_records.append(gap_record)
            if ms_records:
                content_type = f"image/{IMAGE_FORMAT.lower()}"
                for ms_record in ms_records:
                    question_key = ms_record["question_number"]
                    ms_upload_name = ms_record["upload_name"]
                    ms_storage_path = (
                        f"{STORAGE_PREFIX}/{qp.subject}/{qp.year}/{ms_upload_name}"
                    )

                    def set_ms_images(url):
                        if question_key == "ALL":
                            for qn in qp_labels:
                                ms_images[qn] = url
                        elif question_key == "GAP":
                            for qn in qp_labels - ms_labels:
                                ms_images[qn] = url
                        else:
                            ms_images[question_key] = url

                    if storage == "supabase":
                        if ms_storage_path in manifest_rows:
                            image_url = manifest_rows[ms_storage_path]["image_url"]
                            set_ms_images(image_url)
                            continue
                        try:
                            response = upload_image(
                                ms_storage_path,
                                encode_image(ms_record["image"]),
                                content_type,
                            )
                            if response.status_code in (401, 403):
                                raise RuntimeError(
                                    "Upload rejected (HTTP "
                                    f"{response.status_code}): storage policy "
                                    "does not allow this upload."
                                )
                            message = response.text.lower()
                            if (
                                "duplicate" in message
                                or "already exists" in message
                                or response.status_code in (200, 201)
                            ):
                                image_url = storage_public_url(ms_storage_path)
                                set_ms_images(image_url)
                            else:
                                raise RuntimeError(
                                    f"HTTP {response.status_code}: "
                                    f"{response.text[:200]}"
                                )
                        except (requests.RequestException, RuntimeError) as error:
                            stats["failed"] += 1
                            log.error(
                                "Markscheme upload failed for %s: %s",
                                ms_storage_path, error,
                            )
                            continue
                        ms_row = {
                            "subject": qp.subject,
                            "year": qp.year,
                            "session": qp.session or "",
                            "paper": paper_number or "",
                            "page_number": ms_record["page_number"],
                            "question_number": question_key,
                            "max_marks": "",
                            "image_url": image_url,
                            "markscheme_image_url": "",
                            "topic_tags": [],
                            "raw_text": "",
                            "markscheme_link": ms_url,
                            "storage_path": ms_storage_path,
                            "kind": "markscheme",
                        }
                        if manifest_file is not None:
                            with open(manifest_file, "a", encoding="utf-8") as handle:
                                handle.write(json.dumps(ms_row) + "\n")
                    else:
                        try:
                            out_path = (
                                Path(local_dir)
                                / qp.subject
                                / str(qp.year)
                                / ms_upload_name
                            )
                            out_path.parent.mkdir(parents=True, exist_ok=True)
                            ms_record["image"].save(out_path)
                            set_ms_images(
                                os.path.relpath(out_path, os.getcwd()).replace(
                                    os.sep, "/"
                                )
                            )
                            stats["uploaded"] += 1
                        except Exception as error:
                            stats["failed"] += 1
                            log.error(
                                "Markscheme local save failed for %s: %s",
                                out_path, error,
                            )
            else:
                log.warning(
                    "No markscheme text layer for %s; skipping markscheme images",
                    ms_entry.filename,
                )

        for record in records:
            upload_name = record["upload_name"]
            sliced = record["image"]
            question_number = record["question_number"]
            raw_text = record["raw_text"]
            page_number = record["page_number"]
            storage_path = f"{STORAGE_PREFIX}/{qp.subject}/{qp.year}/{upload_name}"
            if storage == "supabase":
                if storage_path in manifest_rows:
                    reused = dict(manifest_rows[storage_path])
                    reused.setdefault("markscheme_image_url", "")
                    reused["markscheme_image_url"] = ms_images.get(
                        question_number, ""
                    )
                    rows.append(reused)
                    stats["uploaded"] += 1
                    continue
                content_type = f"image/{IMAGE_FORMAT.lower()}"
                try:
                    response = upload_image(
                        storage_path, encode_image(sliced), content_type
                    )
                    if response.status_code in (401, 403):
                        raise RuntimeError(
                            f"Upload rejected (HTTP {response.status_code}): the "
                            "publishable key needs a storage policy allowing "
                            "insert on 'past-papers', or use the secret key."
                        )
                    message = response.text.lower()
                    if "duplicate" in message or "already exists" in message:
                        image_url = storage_public_url(storage_path)
                    elif response.status_code in (200, 201):
                        image_url = storage_public_url(storage_path)
                    else:
                        raise RuntimeError(
                            f"HTTP {response.status_code}: {response.text[:200]}"
                        )
                except requests.RequestException as error:
                    stats["failed"] += 1
                    log.error("Upload failed for %s: %s", storage_path, error)
                    continue
                except RuntimeError as error:
                    stats["failed"] += 1
                    log.error("Upload failed for %s: %s", storage_path, error)
                    continue
                row = {
                    "subject": qp.subject,
                    "year": qp.year,
                    "session": qp.session or "",
                    "paper": paper_number or "",
                    "page_number": page_number,
                    "question_number": question_number,
                    "max_marks": record.get("max_marks") or "",
                    "image_url": image_url,
                    "markscheme_image_url": ms_images.get(question_number, ""),
                    "topic_tags": [],
                    "raw_text": raw_text,
                    "markscheme_link": ms_url,
                    "storage_path": storage_path,
                }
                rows.append(row)
                stats["uploaded"] += 1
                if manifest_file is not None:
                    with open(manifest_file, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row) + "\n")
                continue
            if local_dir:
                try:
                    out_path = (
                        Path(local_dir) / qp.subject / str(qp.year) / upload_name
                    )
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    sliced.save(out_path)
                    image_url = os.path.relpath(out_path, os.getcwd()).replace(
                        os.sep, "/"
                    )
                    stats["uploaded"] += 1
                except Exception as error:
                    stats["failed"] += 1
                    log.error("Local save failed for %s: %s", out_path, error)
                    continue
            else:
                try:
                    data = encode_image(sliced)
                    supabase.storage.from_(BUCKET_NAME).upload(
                        storage_path,
                        data,
                        file_options={
                            "content-type": f"image/{IMAGE_FORMAT.lower()}"
                        },
                    )
                    image_url = supabase.storage.from_(
                        BUCKET_NAME
                    ).get_public_url(storage_path)
                except Exception as error:
                    message = str(error).lower()
                    if "duplicate" in message or "already exists" in message:
                        image_url = supabase.storage.from_(
                            BUCKET_NAME
                        ).get_public_url(storage_path)
                    else:
                        stats["failed"] += 1
                        log.error("Upload failed for %s: %s", storage_path, error)
                        continue

            row = {
                "subject": qp.subject,
                "year": qp.year,
                "page_number": page_number,
                "question_number": question_number,
                "image_url": image_url,
                "topic_tags": [],
                "raw_text": raw_text,
                "markscheme_link": ms_url,
            }
            if local_dir:
                row.update(
                    {
                        "session": qp.session or "",
                        "paper": paper_number or "",
                        "max_marks": record.get("max_marks") or "",
                        "markscheme_image_url": ms_images.get(
                            question_number, ""
                        ),
                    }
                )
                rows.append(row)
                continue
            try:
                supabase.table("questions").insert(row).execute()
                stats["uploaded"] += 1
            except Exception as error:
                stats["failed"] += 1
                log.error("Insert failed for %s: %s", storage_path, error)

        log.info(
            "Done %s (%d pages, %d images)",
            qp.filename, len(images), len(records),
        )
        if (
            storage == "supabase"
            and manifest_file is not None
            and stats["failed"] == fails_before
        ):
            marker = {
                "kind": "paper_done",
                "paper_key": list(paper_key),
                "filename": qp.filename,
            }
            with open(manifest_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(marker) + "\n")
            done_papers.add(paper_key)

    if csv_out is not None:
        fieldnames = [
            "subject",
            "year",
            "session",
            "paper",
            "page_number",
            "question_number",
            "max_marks",
            "image_url",
            "markscheme_image_url",
            "topic_tags",
            "raw_text",
            "markscheme_link",
        ]
        with open(csv_out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fieldnames, extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        **row,
                        "topic_tags": "",
                    }
                )
        log.info("Wrote %d rows to %s", len(rows), csv_out)

    log.info(
        "Summary: %d papers, %d slices stored, %d failures, %d papers without "
        "markscheme, %d pages skipped",
        stats["processed"],
        stats["uploaded"],
        stats["failed"],
        stats["missing_ms"],
        stats["skipped_pages"],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Extract IB past papers from dl.pirateib.su into Supabase."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(DEFAULT_LIST_FILE),
        help="List file to read (default: %(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N question papers",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help="Save slice PNGs under this directory instead of uploading to Supabase",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Write a CSV of question rows; images are saved under --local-dir "
        "(default: extracted_samples) and Supabase is skipped",
    )
    parser.add_argument(
        "--storage",
        type=str,
        choices=("local", "supabase"),
        default="local",
        help="With --csv-out: 'local' saves images under --local-dir, "
        "'supabase' uploads them to the past-papers bucket and links public URLs",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSONL manifest of completed uploads for --storage supabase resume",
    )
    parser.add_argument(
        "--subject",
        type=str,
        default=None,
        help="Only process papers of this subject, e.g. Mathematics_AAHL",
    )
    parser.add_argument(
        "--cookies",
        type=str,
        default=None,
        help="Extra cookies, e.g. 'cf_clearance=...; __cf_bm=...'",
    )
    parser.add_argument(
        "--headers",
        type=str,
        default=None,
        help="Extra headers, e.g. 'Referer: https://pirateib.su/; X-Custom: 1'",
    )
    args = parser.parse_args()

    if not args.file.exists():
        parser.error(f"List file not found: {args.file}")

    run(
        list_file=args.file,
        limit=args.limit,
        cookies=parse_cookies_arg(args.cookies),
        headers=parse_headers_arg(args.headers),
        local_dir=args.local_dir,
        csv_out=args.csv_out,
        subject=args.subject,
        storage=args.storage,
        manifest_file=args.manifest,
    )


if __name__ == "__main__":
    main()
