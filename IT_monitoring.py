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
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
WIN_FROM = TODAY - timedelta(days=LOOKBACK)
READ_TIMEOUT = 20
REQUEST_RETRIES = 1
REQUEST_BACKOFF = 0.5
DEBUG = os.getenv("IT_MONITORING_DEBUG", "").lower() in {"1", "true", "yes", "on"}

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
    # Monthly archives are narrower and tend to respond faster than /press/.
    # Keep /press/ as the last fallback so a slow top page does not delay normal runs.
    return [*month_urls(TODAY, LOOKBACK), BASE_URL]


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
        self._recent_text: list[str] = []
        self._current_href: str | None = None
        self._current_title: list[str] = []
        self._current_context_before = ""

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
        self._current_href = None
        self._current_title = []
        self._current_context_before = ""


def iter_press_releases(html: str, base_url: str) -> Iterable[PressRelease]:
    parser = PressListParser()
    parser.feed(html)
    seen_links: set[str] = set()

    for anchor in parser.anchors:
        title = anchor.title
        if not title or not kw_hit(title):
            continue

        date = parse_date(anchor.context_before)
        if not date or date < WIN_FROM or date > TODAY:
            continue

        url = urljoin(base_url, anchor.href)
        if url in seen_links:
            continue
        seen_links.add(url)
        yield PressRelease(dt=date, title=title, url=url)


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


def dedupe_and_sort(releases: Iterable[PressRelease]) -> list[PressRelease]:
    seen: set[tuple[str, str]] = set()
    output: list[PressRelease] = []
    for release in sorted(releases, key=lambda item: item.dt, reverse=True):
        key = (release.title, release.url)
        if key in seen:
            continue
        seen.add(key)
        output.append(release)
    return output


def print_releases(releases: Iterable[PressRelease]) -> None:
    print("【経済産業省ニュースリリース（投資・IT関連）】")
    releases = list(releases)
    if not releases:
        print("該当データなし")
        return
    for release in releases:
        print(f"○{release.date_label}　{release.title}")
        print(f"　{release.url}\n")


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
    releases = dedupe_and_sort(iter_press_releases(sample_html, BASE_URL))
    assert len(releases) == 2, releases
    assert releases[0].title == "成長投資ガイダンス（案）を公表しました"
    assert releases[0].url == "https://www.meti.go.jp/press/2026/06/20260612001/20260612001.html"
    assert month_urls(datetime(2026, 6, 17, tzinfo=JST), LOOKBACK)[0] == "https://www.meti.go.jp/press/archive_202606.html"
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
    print_releases(scrape_press_releases())


if __name__ == "__main__":
    main()
