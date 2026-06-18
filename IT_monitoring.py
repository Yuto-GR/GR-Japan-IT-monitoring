#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
METI press release watcher.

経済産業省「ニュースリリース」から、指定キーワードにマッチする直近の
プレスリリースを抽出して表示します。

外部パッケージに依存せず、この Codex 環境でも `--self-test` で解析ロジックを
検証できるよう、HTTP 取得と HTML 解析は Python 標準ライブラリで実装しています。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

# ───────── Settings ──────────────────────────────────────
BASE_URL = "https://www.meti.go.jp/press/"
LOOKBACK = 14
MEETING_LOOKBACK = 4
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
WIN_FROM = TODAY - timedelta(days=LOOKBACK)
MEETING_WIN_FROM = TODAY - timedelta(days=MEETING_LOOKBACK)
READ_TIMEOUT = 20
REQUEST_RETRIES = 1
REQUEST_BACKOFF = 0.5
DEBUG = os.getenv("IT_MONITORING_DEBUG", "").lower() in {"1", "true", "yes", "on"}
READER_BASE_URL = os.getenv("METI_READER_BASE_URL", "https://r.jina.ai/")
MEETING_URLS = [
    "https://wwws.meti.go.jp/interface/honsho/committee/index.cgi/committee",
    "https://www.meti.go.jp/shingikai/index.html",
]

# ───────── Keywords ──────────────────────────────────────
KEYWORDS = [
    "投資", "成長投資", "設備投資", "対内直接投資", "海外投資", "投資促進",
    # ── 政治・政策 ──
    "デジタル社会推進本部", "経済安全保障対策本部", "経済安全保障推進本部",
    "情報通信戦略調査会", "経済成長戦略本部", "知的財産戦略調査会",
    "競争政策調査会", "プラットフォームサービス", "特定利用者情報",
    "web3", "web3.0研究会", "デジタル社会構想会議", "デジタル臨時行政調査会",
    "デジタル社会推進会議",
    # 注意：「政調」は kw_hit() 関数内で優先的に処理される
    # ── 技術一般 ──
    "デジタル", "情報通信", "サイバー", "AI", "ＤＸ", "DX", "IT", "5g",
    # ── 行政関連 ──
    "標準仕様", "ガイドライン", "無線局", "免許状", "光ファイバ",
    "クラウド", "ガバメントクラウド", "データセンター",
    "経済安全保障", "QUAD", "サプライチェーン", "セキュリティクリアランス",
    "電気通信事業法", "サイバーセキュリティ", "Web3", "半導体",
    "GIGAスクール構想", "量子コンピューター", "スーパーコンピュータ",
    "スマホ新法", "青少年インターネット環境整備法", "Fintech",
    "中央銀行デジタル通貨", "知的財産", "個人情報保護", "医療DX",
    "新年度予算（デジタル関連）", "環境",
]
SHORT_ASCII = {"ai", "it", "dx", "5g"}
DATE_RE = re.compile(r"(\d{4})年\s*0?(\d{1,2})月\s*0?(\d{1,2})日")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass(frozen=True)
class PressRelease:
    dt: datetime
    title: str
    url: str

    @property
    def date_label(self) -> str:
        return f"{self.dt.month}月{self.dt.day}日"


@dataclass(frozen=True)
class AnchorCandidate:
    href: str
    title: str
    context_before: str
    context_after: str = ""


def debug(message: str) -> None:
    if DEBUG:
        print(f"[DBG] {message}", file=sys.stderr)


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def kw_hit(text: str) -> bool:
    normalized = normalize(text)
    # 「政調」は他の短い英数字キーワードより先に判定する。
    if "政調" in normalized:
        return True

    for kw in KEYWORDS:
        keyword = normalize(kw)
        if keyword in SHORT_ASCII:
            if re.search(rf"(?:^|[^a-z0-9]){re.escape(keyword)}(?:[^a-z0-9]|$)", normalized):
                return True
        elif keyword in normalized:
            return True
    return False


def parse_date(text: str) -> datetime | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    return datetime(year, month, day, tzinfo=JST)


def month_urls(today: datetime, lookback_days: int) -> list[str]:
    """Return month archive URLs that can overlap the target window."""
    urls: list[str] = []
    current = today.replace(day=1)
    lower_bound = today - timedelta(days=lookback_days)
    while current >= lower_bound.replace(day=1):
        urls.append(urljoin(BASE_URL, f"archive_{current.year}{current.month:02d}.html"))
        previous_month_last_day = current - timedelta(days=1)
        current = previous_month_last_day.replace(day=1)
    return urls


def candidate_urls() -> list[str]:
    # Reader URLs are tried first because Codespaces may time out when reading
    # directly from www.meti.go.jp, while the reader service can fetch the same
    # public METI page and return lightweight Markdown quickly. Direct METI URLs
    # remain as fallbacks so the script is not dependent on the reader service.
    direct_urls = [*month_urls(TODAY, LOOKBACK), BASE_URL]
    return urls_with_reader_fallbacks(direct_urls)


def meeting_candidate_urls() -> list[str]:
    return urls_with_reader_fallbacks(MEETING_URLS)


def urls_with_reader_fallbacks(direct_urls: list[str]) -> list[str]:
    reader_urls = [reader_url(url) for url in direct_urls if READER_BASE_URL]
    return [*reader_urls, *direct_urls]


def reader_url(url: str) -> str:
    return f"{READER_BASE_URL.rstrip('/')}/{url}"


def original_url(url: str) -> str:
    if READER_BASE_URL and url.startswith(READER_BASE_URL):
        return url[len(READER_BASE_URL):]
    return url


def request_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        "Connection": "close",
    }


def fetch_html(url: str) -> str:
    """Fetch HTML with short retries and a curl fallback for restricted environments."""
    errors: list[str] = []
    for attempt in range(REQUEST_RETRIES + 1):
        debug(f"GET {url} attempt={attempt + 1} timeout={READ_TIMEOUT}")
        start = time.monotonic()
        try:
            request = Request(url, headers=request_headers())
            with urlopen_ipv4(request, timeout=READ_TIMEOUT) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                elapsed = time.monotonic() - start
                debug(f"HTTP {response.status} {url} {len(body)} bytes in {elapsed:.1f}s")
                return body.decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"urllib attempt {attempt + 1}: {exc}")
            debug(f"request failed: {url}: {exc}")
            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_BACKOFF * (attempt + 1))

    curl_html = fetch_html_with_curl(url, errors)
    if curl_html is not None:
        return curl_html

    raise RuntimeError(f"failed to fetch {url}: {'; '.join(errors)}")


def urlopen_ipv4(request: Request, timeout: int):
    """Open a URL while preferring IPv4 to avoid IPv6 stalls in Codespaces."""
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        return urlopen(request, timeout=timeout)
    finally:
        socket.getaddrinfo = original_getaddrinfo


def fetch_html_with_curl(url: str, errors: list[str]) -> str | None:
    """Fallback for environments where urllib is blocked but curl works."""
    curl = shutil.which("curl")
    if not curl:
        errors.append("curl fallback unavailable: curl command not found")
        return None

    command = [
        curl,
        "--location",
        "--fail",
        "--silent",
        "--show-error",
        "--compressed",
        "--http1.1",
        "--ipv4",
        "--max-time",
        str(READ_TIMEOUT),
    ]
    for name, value in request_headers().items():
        command.extend(["--header", f"{name}: {value}"])
    command.append(url)

    debug(f"curl fallback GET {url} timeout={READ_TIMEOUT}")
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=READ_TIMEOUT + 5,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        detail = stderr or f"exit status {exc.returncode}"
        errors.append(f"curl fallback: {detail}")
        debug(f"curl fallback failed: {url}: {detail}")
        return None
    except subprocess.TimeoutExpired as exc:
        detail = f"timed out after {exc.timeout}s"
        errors.append(f"curl fallback: {detail}")
        debug(f"curl fallback failed: {url}: {detail}")
        return None
    except OSError as exc:
        errors.append(f"curl fallback: {exc}")
        debug(f"curl fallback failed: {url}: {exc}")
        return None

    elapsed = time.monotonic() - start
    debug(f"curl fallback OK {url} {len(completed.stdout)} bytes in {elapsed:.1f}s")
    return completed.stdout.decode("utf-8", errors="replace")


class PressListParser(HTMLParser):
    """Collect anchor text and nearby preceding text from METI list HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[AnchorCandidate] = []
        self._events: list[tuple[str, str, str]] = []
        self._recent_text: list[str] = []
        self._current_href: str | None = None
        self._current_title: list[str] = []
        self._current_context_before = ""

    def candidates(self) -> list[AnchorCandidate]:
        candidates: list[AnchorCandidate] = []
        for index, event in enumerate(self._events):
            kind, title, href = event
            if kind != "anchor":
                continue
            before = " ".join(text for text_kind, text, _ in self._events[max(0, index - 40):index] if text_kind == "text")
            after = " ".join(text for text_kind, text, _ in self._events[index + 1:index + 41] if text_kind == "text")
            candidates.append(AnchorCandidate(href=href, title=title, context_before=before, context_after=after))
        return candidates

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._current_href = href
            self._current_title = []
            self._current_context_before = " ".join(self._recent_text[-40:])

    def handle_data(self, data: str) -> None:
        text = " ".join(unescape(data).split())
        if not text:
            return
        self._recent_text.append(text)
        self._events.append(("text", text, ""))
        if len(self._recent_text) > 80:
            self._recent_text = self._recent_text[-80:]
        if self._current_href:
            self._current_title.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        title = " ".join(self._current_title).strip()
        if title:
            self.anchors.append(AnchorCandidate(
                href=self._current_href,
                title=title,
                context_before=self._current_context_before,
            ))
            self._events.append(("anchor", title, self._current_href))
        self._current_href = None
        self._current_title = []
        self._current_context_before = ""


def iter_press_releases(
    html: str,
    base_url: str,
    win_from: datetime = WIN_FROM,
    win_to: datetime | None = TODAY,
) -> Iterable[PressRelease]:
    seen_links: set[str] = set()
    source_base_url = original_url(base_url)

    for anchor in iter_anchor_candidates(html):
        title = anchor.title
        if not title or not kw_hit(title):
            continue

        date = parse_date(anchor.context_before) or parse_date(anchor.context_after)
        if not date or date < win_from:
            continue
        if win_to is not None and date > win_to:
            continue

        url = urljoin(source_base_url, anchor.href)
        if url in seen_links:
            continue
        seen_links.add(url)
        yield PressRelease(dt=date, title=title, url=url)


def iter_anchor_candidates(content: str) -> Iterable[AnchorCandidate]:
    parser = PressListParser()
    parser.feed(content)
    yield from parser.candidates()
    yield from iter_markdown_anchor_candidates(content)


def iter_markdown_anchor_candidates(markdown: str) -> Iterable[AnchorCandidate]:
    current_date_text = ""
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if parse_date(stripped):
            current_date_text = stripped
        for match in MARKDOWN_LINK_RE.finditer(stripped):
            title = " ".join(unescape(match.group(1)).split())
            href = match.group(2).strip()
            if title and href:
                after_text = markdown[match.end():match.end() + 300]
                yield AnchorCandidate(href=href, title=title, context_before=current_date_text, context_after=after_text)


def scrape_press_releases() -> list[PressRelease]:
    errors: list[str] = []
    for url in candidate_urls():
        try:
            html = fetch_html(url)
            releases = list(iter_press_releases(html, url))
            if releases:
                debug(f"matched {len(releases)} item(s) from {url}")
                return dedupe_and_sort(releases)
            debug(f"no matching item from {url}; trying next candidate")
        except RuntimeError as exc:
            errors.append(str(exc))
            debug(str(exc))

    if errors:
        warn("経済産業省ニュースリリースを取得できませんでした。")
        warn("取得先URLとエラーを確認するには IT_MONITORING_DEBUG=1 で再実行してください。")
        for error in errors[:3]:
            warn(error)
        for error in errors[3:]:
            debug(error)
    return []


def scrape_meetings() -> list[PressRelease]:
    errors: list[str] = []
    items: list[PressRelease] = []
    for url in meeting_candidate_urls():
        try:
            html = fetch_html(url)
            releases = list(iter_press_releases(html, url, win_from=MEETING_WIN_FROM, win_to=None))
            if releases:
                debug(f"matched {len(releases)} meeting item(s) from {url}")
                items.extend(releases)
            else:
                debug(f"no matching meeting item from {url}; trying next candidate")
        except RuntimeError as exc:
            errors.append(str(exc))
            debug(str(exc))

    if errors and not items:
        warn("経済産業省の審議会・研究会等を取得できませんでした。")
        for error in errors[:3]:
            warn(error)
        for error in errors[3:]:
            debug(error)
    return dedupe_and_sort(items, reverse=False)


def dedupe_and_sort(releases: Iterable[PressRelease], reverse: bool = True) -> list[PressRelease]:
    seen: set[tuple[str, str]] = set()
    output: list[PressRelease] = []
    for release in sorted(releases, key=lambda item: item.dt, reverse=reverse):
        key = (release.title, release.url)
        if key in seen:
            continue
        seen.add(key)
        output.append(release)
    return output


def print_section(title: str, releases: Iterable[PressRelease]) -> None:
    print(title)
    releases = list(releases)
    if not releases:
        print("該当データなし")
        return
    for release in releases:
        print(f"○{release.date_label}　{release.title}")
        print(f"　{release.url}\n")


def print_releases(press_releases: Iterable[PressRelease], meeting_releases: Iterable[PressRelease]) -> None:
    print_section("【経済産業省ニュースリリース（投資・IT関連）】", press_releases)
    print()
    print_section("【審議会・研究会等】", meeting_releases)


def run_self_test() -> None:
    sample_html = """
    <html><body><ul>
      <li><span>2026年6月12日</span>
        <a href="/press/2026/06/20260612001/20260612001.html">成長投資ガイダンス（案）を公表しました</a>
      </li>
      <li><span>2026年6月11日</span>
        <a href="/press/2026/06/ignored.html">関係ない発表</a>
      </li>
      <li><span>2026年6月10日</span>
        <a href="/press/2026/06/ai.html">AI政策に関するガイドラインを改定しました</a>
      </li>
    </ul></body></html>
    """
    sample_markdown = """
    2026年6月15日
    [AI分野を中心とした新たな五庁協力について合意しました](https://www.meti.go.jp/press/2026/06/20260615002/20260615002.html)
    """
    sample_meeting_html = """
    <html><body><ul>
      <li><a href="/interface/honsho/committee/detail.cgi?committee_id=1">第１回デジタルプラットフォームの透明性・公正性に関するモニタリング会合</a>
      <span>2026年6月30日(火)</span></li>
    </ul></body></html>
    """
    releases = dedupe_and_sort([
        *iter_press_releases(sample_html, BASE_URL),
        *iter_press_releases(sample_markdown, reader_url(BASE_URL)),
    ])
    meeting_releases = [
        *iter_press_releases(sample_meeting_html, MEETING_URLS[0], win_from=MEETING_WIN_FROM, win_to=None)
    ]
    assert len(releases) == 3, releases
    assert len(meeting_releases) == 1, meeting_releases
    assert meeting_releases[0].title == "第１回デジタルプラットフォームの透明性・公正性に関するモニタリング会合"
    assert releases[0].title == "AI分野を中心とした新たな五庁協力について合意しました"
    assert releases[0].url == "https://www.meti.go.jp/press/2026/06/20260615002/20260615002.html"
    assert releases[1].title == "成長投資ガイダンス（案）を公表しました"
    assert releases[1].url == "https://www.meti.go.jp/press/2026/06/20260612001/20260612001.html"
    assert month_urls(datetime(2026, 6, 17, tzinfo=JST), LOOKBACK)[0] == "https://www.meti.go.jp/press/archive_202606.html"
    assert candidate_urls()[0].startswith("https://r.jina.ai/https://www.meti.go.jp/press/archive_")
    assert kw_hit("政調でデジタル政策を議論")
    assert kw_hit("ローカル5Gの無線局免許状を交付")
    assert not kw_hit("baitという英単語だけでは一致しない")
    print("self-test ok")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="METI press release watcher")
    parser.add_argument("--self-test", action="store_true", help="run offline parser tests and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return

    debug("経済産業省プレスリリース『投資・IT』関連情報取得開始")
    press_releases = scrape_press_releases()
    meeting_releases = scrape_meetings()
    print_releases(press_releases, meeting_releases)


if __name__ == "__main__":
    main()
