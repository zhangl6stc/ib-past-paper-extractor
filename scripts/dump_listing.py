import argparse
import logging
import re
import time
from urllib.parse import urljoin, unquote

import common
import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

BASE_URL = "https://dl.pirateib.su/IB PAST PAPERS - YEAR/"
ROOT_MARKER = "IB PAST PAPERS - YEAR"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HREF_RE = re.compile(r'href="([^"]+)"')
CHALLENGE_HINT = b"Just a moment"
DEFAULT_OUT = str(common.LISTS / "by_year_file_list.txt")
DEFAULT_MAX_DEPTH = 12
DEFAULT_DELAY = 0.2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dump")


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


def build_session(cookies=None, headers=None):
    if cffi_requests is not None:
        session = cffi_requests.Session(impersonate="chrome")
    else:
        session = requests.Session()
        session.headers["User-Agent"] = BROWSER_UA
    if headers:
        session.headers.update(headers)
    if cookies:
        session.cookies.update(cookies)
    return session


def fetch_html(session, url):
    response = session.get(url, timeout=60)
    content = response.content
    if response.status_code in (403, 429) and CHALLENGE_HINT in content[:2000]:
        raise RuntimeError(
            "Cloudflare challenge at {url}. Open the site in a browser, copy the "
            "cf_clearance cookie from DevTools, and pass it via --cookies."
        )
    response.raise_for_status()
    return response.text


def walk(session, url, rel_dir, out_lines, max_depth, delay, visited, depth=0):
    if url in visited or depth > max_depth:
        return
    visited.add(url)
    html = fetch_html(session, url)

    directories = []
    files = []
    for raw in HREF_RE.findall(html):
        clean = raw.split("?")[0]
        name = unquote(clean)
        if not name or name.startswith(("/", "#", "javascript:")):
            continue
        if name in ("..", "../", "./"):
            continue
        if name.endswith("/"):
            directories.append((clean.rstrip("/"), name.rstrip("/")))
        elif name.lower().endswith(".pdf"):
            files.append(name)

    if files:
        windows_dir = (
            "D:\\International Baccalaureate Documents\\" + ROOT_MARKER
        )
        if rel_dir:
            windows_dir += "\\" + "\\".join(rel_dir)
        out_lines.append(" Directory of " + windows_dir)
        out_lines.append("")
        for filename in sorted(files):
            out_lines.append("        " + filename)
        out_lines.append("")
        log.info("Found %d pdfs in %s", len(files), windows_dir)

    for encoded, decoded in sorted(directories, key=lambda item: item[1]):
        time.sleep(delay)
        child_url = urljoin(url, encoded + "/")
        walk(
            session,
            child_url,
            rel_dir + (decoded,),
            out_lines,
            max_depth,
            delay,
            visited,
            depth + 1,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Recursively dump the dl.pirateib.su directory listing "
        "into a dir /s style file for extract_questions.py."
    )
    parser.add_argument("--base", default=BASE_URL, help="Starting directory URL")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output file")
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
        help="Extra headers, e.g. 'Referer: https://pirateib.su/'",
    )
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = parser.parse_args()

    session = build_session(
        cookies=parse_cookies_arg(args.cookies),
        headers=parse_headers_arg(args.headers),
    )
    out_lines = []
    visited = set()
    walk(
        session,
        args.base,
        (),
        out_lines,
        args.max_depth,
        args.delay,
        visited,
    )

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out_lines))
        handle.write("\n")
    log.info("Wrote %d lines to %s", len(out_lines), args.out)


if __name__ == "__main__":
    main()
