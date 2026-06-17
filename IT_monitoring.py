#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meti_shingikai_scraper.py  rev-2.2-DBG  (2025-06-18)

■ 目的
  経済産業省「審議会・研究会（新着情報）」ページ（動的描画）から
  指定キーワードにマッチする過去4日分の会議案内を抽出して表示します。
  ネットワーク・レンダリング周りのトラブルシュート用にデバッグログを大量に出力します。

■ 依存パッケージ
  pip install playwright beautifulsoup4
  playwright install chromium
"""

import re
import sys
import socket
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ───────── Settings ──────────────────────────────────────
BASE_URL = "https://www.meti.go.jp/shingikai/"
LOOKBACK = 4
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
WIN_FROM = TODAY - timedelta(days=LOOKBACK)

# ───────── Keywords ──────────────────────────────────────
KEYWORDS = [
    "DX", "デジタル", "クラウド", "ガバメントクラウド", "データセンター",
    "経済安全保障", "QUAD", "サプライチェーン", "セキュリティクリアランス",
    "電気通信事業法", "サイバーセキュリティ", "Web3", "半導体", "AI",
    "GIGAスクール構想", "量子コンピューター", "スーパーコンピュータ",
    "スマホ新法", "青少年インターネット環境整備法", "Fintech",
    "中央銀行デジタル通貨", "知的財産", "個人情報保護", "医療DX",
    "新年度予算（デジタル関連）", "環境"
]
SHORT_ASCII = {"ai", "it", "dx"}

def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()

def kw_hit(text: str) -> bool:
    t = normalize(text)
    for kw in KEYWORDS:
        k = normalize(kw)
        if k in SHORT_ASCII:
            if re.search(rf"(?:^|[^a-z0-9]){k}(?:[^a-z0-9]|$)", t):
                return True
        elif k in t:
            return True
    return False

# ───────── Date parsing ──────────────────────────────────
DATE_RE = re.compile(r"(\d{4})年\s*0?(\d{1,2})月\s*0?(\d{1,2})日")

def parse_date(s: str):
    m = DATE_RE.search(s)
    if not m:
        return None
    y, mth, d = map(int, m.groups())
    return datetime(y, mth, d, tzinfo=JST)

# ───────── Fetch dynamic HTML via Playwright ─────────────
def fetch_html_dynamic(url: str) -> str:
    # ── ネットワーク接続確認
    try:
        socket.create_connection(("www.google.com", 80), timeout=5)
        print("[DBG] network check: OK", file=sys.stderr)
    except Exception as e:
        print(f"[DBG] network check failed: {e}", file=sys.stderr)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        ))
        # ブロックして高速化
        ctx.route("**/*.{png,jpg,jpeg,gif,svg,webp,css,js,woff,woff2}",
                  lambda route: route.abort())
        page = ctx.new_page()
        # ── Playwright.goto デバッグ
        try:
            print(f"[DBG] playwright.goto start for {url}", file=sys.stderr)
            page.goto(url, wait_until="commit", timeout=60000)
            print("[DBG] playwright.goto succeeded", file=sys.stderr)
        except Exception as e:
            print(f"[DBG] playwright.goto failed: {e}", file=sys.stderr)
            browser.close()
            raise
        # ── 一覧要素の待機
        try:
            print("[DBG] waiting for selector ul.shingikaiList li", file=sys.stderr)
            page.wait_for_selector("ul.shingikaiList li", timeout=60000)
            print("[DBG] selector appeared", file=sys.stderr)
        except Exception as e:
            print(f"[DBG] wait_for_selector failed: {e}", file=sys.stderr)
        html = page.content()
        browser.close()
        return html

# ───────── Main scraper ──────────────────────────────────
def scrape_shingikai():
    html = fetch_html_dynamic(BASE_URL)
    # ── HTMLプレースホルダー検出
    if "Javascriptを有効にしてください" in html:
        print("[DBG] placeholder HTML detected", file=sys.stderr)
    # ── HTML情報
    print(f"[DBG] fetched HTML length: {len(html)}", file=sys.stderr)
    print(f"[DBG] head snippet: {html[:200]!r}", file=sys.stderr)

    soup = BeautifulSoup(html, "html.parser")
    items = []
    for li in soup.select("ul.shingikaiList li"):
        span = li.find("span", class_="date")
        if not span:
            print("[DBG] no date span in li", str(li)[:100], file=sys.stderr)
            continue
        dt = parse_date(span.get_text())
        if not dt:
            print(f"[DBG] parse_date failed on '{span.get_text()}'", file=sys.stderr)
            continue
        if dt < WIN_FROM or dt > TODAY:
            print(f"[DBG] date {dt} out of window", file=sys.stderr)
            continue

        a = li.find("a", href=True)
        if not a:
            print("[DBG] no link in li", file=sys.stderr)
            continue
        title = a.get_text(" ", strip=True)
        if not kw_hit(title):
            print(f"[DBG] title no KW: {title}", file=sys.stderr)
            continue

        url = urljoin(BASE_URL, a["href"])
        items.append({
            "dt": dt,
            "date": dt.strftime("%-m月%-d日"),
            "title": title,
            "url": url
        })

    # ── dedupe & sort
    seen = set(); out = []
    for rec in sorted(items, key=lambda x: x["dt"], reverse=True):
        key = (rec["date"], rec["title"])
        if key in seen:
            print(f"[DBG] duplicate {key}", file=sys.stderr)
            continue
        seen.add(key); out.append(rec)
    return out

# ───────── CLI ─────────────────────────────────────────
def main():
    recs = scrape_shingikai()
    print("【審議会・研究会（新着情報）】")
    if not recs:
        print("該当データなし"); return
    for r in recs:
        print(f"○{r['date']}　{r['title']}")
        print(f"　{r['url']}\n")

if __name__ == "__main__":
    main()
