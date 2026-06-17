#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
METI press release watcher.

経済産業省「ニュースリリース」から、指定キーワードにマッチする直近の
プレスリリースを抽出して表示します。

`https://www.meti.go.jp/press/` は静的 HTML でも最新リリースを返すため、
Playwright は使わず requests + BeautifulSoup で取得します。トップページが
遅い・応答しない場合は当月/前月の月別アーカイブへフォールバックします。
"""

from __future__ import annotations

import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ───────── Settings ──────────────────────────────────────
BASE_URL = "https://www.meti.go.jp/press/"
LOOKBACK = 14
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
WIN_FROM = TODAY - timedelta(days=LOOKBACK)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 90
REQUEST_RETRIES = 3
REQUEST_BACKOFF = 1.5

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
        urls.append(urljoin(BASE_URL, f"{current.year}/{current.month:02d}/"))
        previous_month_last_day = current - timedelta(days=1)
        current = previous_month_last_day.replace(day=1)
    return urls


def candidate_urls() -> list[str]:
    # Try the normal page first, then monthly archives. The monthly pages are useful
    # when /press/ is slow from GitHub Actions or a local corporate network.
    return [BASE_URL, *month_urls(TODAY, LOOKBACK)]


def make_session() -> Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        "Connection": "close",
    })
    retry = Retry(
        total=REQUEST_RETRIES,
        connect=REQUEST_RETRIES,
        read=REQUEST_RETRIES,
        status=REQUEST_RETRIES,
        backoff_factor=REQUEST_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_html(session: Session, url: str) -> str:
    print(f"[DBG] GET {url} timeout=({CONNECT_TIMEOUT}, {READ_TIMEOUT})", file=sys.stderr)
    start = time.monotonic()
    response: Response = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    elapsed = time.monotonic() - start
    print(f"[DBG] HTTP {response.status_code} {url} {len(response.content)} bytes in {elapsed:.1f}s", file=sys.stderr)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def iter_press_releases(html: str, base_url: str) -> Iterable[PressRelease]:
    soup = BeautifulSoup(html, "html.parser")
    seen_links: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        title = anchor.get_text(" ", strip=True)
        if not title or not kw_hit(title):
            continue

        date = find_nearby_date(anchor)
        if not date or date < WIN_FROM or date > TODAY:
            continue

        url = urljoin(base_url, anchor["href"])
        if url in seen_links:
            continue
        seen_links.add(url)
        yield PressRelease(dt=date, title=title, url=url)


def find_nearby_date(anchor) -> datetime | None:
    # METI press lists usually have the date in the same list item/parent block,
    # before the title. Search a compact surrounding text block first.
    for parent in [anchor.parent, anchor.find_parent("li"), anchor.find_parent("dl"), anchor.find_parent("div")]:
        if parent:
            parsed = parse_date(parent.get_text(" ", strip=True))
            if parsed:
                return parsed

    # Fallback for simple text order: look at a few previous siblings.
    sibling = anchor.previous_sibling
    checked = 0
    while sibling is not None and checked < 6:
        text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling)
        parsed = parse_date(text)
        if parsed:
            return parsed
        sibling = sibling.previous_sibling
        checked += 1
    return None


def scrape_press_releases() -> list[PressRelease]:
    errors: list[str] = []
    with make_session() as session:
        for url in candidate_urls():
            try:
                html = fetch_html(session, url)
                releases = list(iter_press_releases(html, url))
                if releases:
                    print(f"[DBG] matched {len(releases)} item(s) from {url}", file=sys.stderr)
                    return dedupe_and_sort(releases)
                print(f"[DBG] no matching item from {url}; trying next candidate", file=sys.stderr)
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
                print(f"[DBG] request failed: {url}: {exc}", file=sys.stderr)

    if errors:
        print("[DBG] all candidate URLs failed or had no matches", file=sys.stderr)
        for error in errors:
            print(f"[DBG] {error}", file=sys.stderr)
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


def main() -> None:
    print("経済産業省プレスリリース『投資・IT』関連情報取得開始...", file=sys.stderr)
    releases = scrape_press_releases()
    print("【経済産業省ニュースリリース（投資・IT関連）】")
    if not releases:
        print("該当データなし")
        return
    for release in releases:
        print(f"○{release.date_label}　{release.title}")
        print(f"　{release.url}\n")


if __name__ == "__main__":
    main()
