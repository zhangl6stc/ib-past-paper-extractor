import argparse
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import common
import requests

BASE_URL = "https://ibdocs.re"
SUBJECTS = [
    "mathematics",
    "mathematics-analysis-and-approaches",
    "economics",
    "physics",
    "computer-science",
]
ROOT_MARKER = "IB PAST PAPERS - YEAR"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEFAULT_OUT = str(common.LISTS / "by_year_file_list.txt")
DEFAULT_WORKERS = 16

DOC_HREF_RE = re.compile(r'href="/documents/([a-z0-9\-]+)"')
CONTENT_URL_RE = re.compile(r'"contentUrl":"([^"]+)"')
RAW_URL_RE = re.compile(r'raw_url:"([^"]+)"')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dump")


def subject_page_url(slug):
    if slug.startswith("http"):
        return slug
    return f"{BASE_URL}/past-papers-by-subject/{slug}"


def document_url(slug):
    return f"{BASE_URL}/documents/{slug}"


def extract_doc_slugs(html):
    return list(dict.fromkeys(DOC_HREF_RE.findall(html)))


def document_path(session, slug):
    try:
        response = session.get(document_url(slug), timeout=60)
        response.raise_for_status()
        html = response.text
    except requests.RequestException as error:
        log.warning("Failed to fetch %s: %s", slug, error)
        return None

    match = CONTENT_URL_RE.search(html)
    if match:
        return unquote(match.group(1))
    match = RAW_URL_RE.search(html)
    if match:
        return unquote(match.group(1))
    log.warning("No PDF path found on %s", slug)
    return None


def to_windows_path(source):
    idx = source.find(ROOT_MARKER)
    if idx == -1:
        return None
    rest = source[idx + len(ROOT_MARKER):].strip("/")
    parts = rest.split("/")
    return "D:\\International Baccalaureate Documents\\" + ROOT_MARKER + "\\" + "\\".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Dump ibdocs.re past-paper listings into a full-path list "
        "file for extract_questions.py."
    )
    parser.add_argument(
        "--subjects",
        type=str,
        default=",".join(SUBJECTS),
        help="Comma-separated subject slugs or page URLs (default: %(default)s)",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output file")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only fetch the first N documents per subject",
    )
    parser.add_argument(
        "--headers",
        type=str,
        default=None,
        help="Extra headers, e.g. 'X-Custom: 1'",
    )
    args = parser.parse_args()

    headers = {}
    if args.headers:
        for item in args.headers.split(";"):
            if ":" in item:
                name, value = item.strip().split(":", 1)
                headers[name.strip()] = value.strip()

    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})
    session.headers.update(headers)

    paths = set()
    for slug in args.subjects.split(","):
        slug = slug.strip()
        if not slug:
            continue
        try:
            response = session.get(subject_page_url(slug), timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log.error("Failed to fetch subject page %s: %s", slug, error)
            continue
        slugs = extract_doc_slugs(response.text)
        log.info("Subject %s: %d documents", slug, len(slugs))
        if args.limit is not None:
            slugs = slugs[: args.limit]

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(document_path, session, s): s for s in slugs}
            for count, future in enumerate(as_completed(futures), start=1):
                source = future.result()
                if source:
                    windows_path = to_windows_path(source)
                    if windows_path:
                        paths.add(windows_path)
                if count % 100 == 0:
                    log.info("Subject %s: %d/%d processed", slug, count, len(slugs))

    with open(args.out, "w", encoding="utf-8") as handle:
        for windows_path in sorted(paths):
            handle.write(windows_path + "\n")
    log.info("Wrote %d paths to %s", len(paths), args.out)


if __name__ == "__main__":
    main()
