#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
speech_watcher.py  (2025-06-23 改訂版)

デジタル庁「大臣等会見」ページから過去4日以内の会見を抽出し、
各会見ページに埋め込まれた YouTube 動画のリンクと再生時間を取得して表示します。

再生時間が取得できない場合はプレースホルダーを出力し、
その下に該当の会見ページリンクも表示します。
"""

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ───────── 定数
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
LOOKBACK_DAYS = 4
WINDOW_START = TODAY - timedelta(days=LOOKBACK_DAYS)

BASE_URL = "https://www.digital.go.jp"
LIST_URL = f"{BASE_URL}/speech"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    )
}

REIWA_RE = re.compile(r"令和(\d+)年(\d+)月(\d+)日")

def parse_iso8601_duration(duration: str) -> int:
    m = re.match(r'PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?', duration)
    if not m:
        return 0
    h = int(m.group('h') or 0)
    mi = int(m.group('m') or 0)
    s = int(m.group('s') or 0)
    return h * 3600 + mi * 60 + s

def fetch_speech_items():
    resp = requests.get(LIST_URL, headers=UA, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    for a in soup.select("a[href^='/speech/minister']"):
        text = a.get_text(" ", strip=True)
        m = REIWA_RE.search(text)
        if not m:
            continue
        era, month, day = map(int, m.groups())
        year = 2018 + era
        dt = datetime(year, month, day, tzinfo=JST)
        if not (WINDOW_START <= dt <= TODAY):
            continue

        title = re.sub(r"（.*?）", "", text)
        prefix = title[title.find("大臣"):] if "大臣" in title else title
        url = urljoin(BASE_URL, a["href"])
        items.append({"date": dt, "prefix": prefix, "page_url": url})
    return items

def lookup_youtube_in_speech(page_url: str):
    resp = requests.get(page_url, headers=UA, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    iframe = soup.find("iframe", src=re.compile(r"youtube\.com/embed/"))
    if iframe:
        src = iframe["src"]
        if src.startswith("//"):
            src = "https:" + src
        vid = src.rsplit("/", 1)[-1].split("?")[0]
    else:
        a = soup.find("a", href=re.compile(r"(youtu\.be/|youtube\.com/watch)"))
        if not a:
            return None, None
        href = a["href"]
        if "youtu.be/" in href:
            vid = href.split("youtu.be/")[1].split("?")[0]
        else:
            vid = href.split("v=")[1].split("&")[0]

    short_url = f"https://youtu.be/{vid}"
    watch_url = f"https://www.youtube.com/watch?v={vid}"
    r2 = requests.get(watch_url, headers=UA, timeout=10)
    r2.raise_for_status()
    meta = BeautifulSoup(r2.text, "html.parser").find("meta", itemprop="duration")
    if not meta or not meta.get("content"):
        return short_url, None
    total_sec = parse_iso8601_duration(meta["content"])
    return short_url, total_sec

def format_duration(sec: int) -> str:
    m, s = divmod(sec or 0, 60)
    return f"{m}分{s}秒"

def main():
    items = fetch_speech_items()
    if not items:
        print("該当データなし")
        return

    print("【平将明デジタル大臣】")
    for it in items:
        date_str = f"{it['date'].month}月{it['date'].day}日"
        prefix = it["prefix"]
        page_url = it["page_url"]
        yt_url, length = lookup_youtube_in_speech(page_url)

        if yt_url and length is not None:
            print(f"○{date_str}の{prefix}（{format_duration(length)}）")
            print(f"　{yt_url}")
        elif yt_url:
            print(f"○{date_str}の{prefix}（再生時間情報を自分で取得してください）")
            print(f"　{yt_url}")
            print(f"　（会見ページから自分で確認して！！！: {page_url}）\n")
        else:
            print(f"○{date_str}の{prefix}（！！！！再生時間情報を自分で取得してください！！！！！！）")
            print(f"　（会見ページから自分で確認して！！！: {page_url}）\n")

        time.sleep(0.2)

if __name__ == "__main__":
    main()

#============自民党＝＝＝＝＝＝＝＝＝＝＝＝＝＝
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ldp_watcher.py  rev‑4.6‑LDP‑r4  (2025‑06‑17)

■ 自民党サイト（/activity）を巡回し，
   過去 4 日＋当日＋未来 10 日の 15 日分から
   デジタル政策関連イベントのみ抽出して表示。
   ─ 重複タイトルは「本文が詳しい方」を優先して 1 行に集約。
"""

# ───────── Imports ──────────────────────────────────────────
import re, time, sys, requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ───────── Global settings ─────────────────────────────────
LOOKBACK          = 4           # 過去 4 日
AHEAD             = 10          # 未来 10 日
WAIT_SEC          = 1
DEBUG             = True
DEBUG_SOU         = True
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

# ───────── キーワード ────────────────────────────────────
KEYWORDS = [
    # ── 政治・政策 ──
    "デジタル社会推進本部","経済安全保障対策本部","経済安全保障推進本部",
    "情報通信戦略調査会","経済成長戦略本部","知的財産戦略調査会",
    "競争政策調査会","プラットフォームサービス","特定利用者情報",
    "web3","web3.0研究会","デジタル社会構想会議","デジタル臨時行政調査会",
    "デジタル社会推進会議",
    # ── 技術一般 ──
    "デジタル","情報通信","サイバー","AI","ＤＸ","DX","IT","5g",
    # ── 行政関連 ──
    "標準仕様","ガイドライン","無線局","免許状","光ファイバ",
    # ── 大臣会見 ──
    "平デジタル大臣",
]
SHORT_ASCII = {"ai", "it", "dx"}          # 2 文字英語は単語境界を意識
norm  = lambda s: re.sub(r"\s+", "", s).lower()
def kw_hit(text: str) -> bool:
    t = norm(text)
    for k in KEYWORDS:
        kl = k.lower()
        if kl in SHORT_ASCII:
            if re.search(rf"(?:^|[^a-z0-9]){kl}(?:[^a-z0-9]|$)", t):
                return True
        elif kl in t:
            return True
    return False

# ───────── 日付ユーティリティ ─────────────────────────
JST   = timezone(timedelta(hours=9))
today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)

# 過去 LOOKBACK 日 ～ 当日 ～ 未来 AHEAD 日
DATES = [today - timedelta(days=delta)
         for delta in range(-AHEAD, LOOKBACK + 1)]

DATE_TAG = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DATE_TXT = re.compile(r"(\d{4})年\s*0?(\d{1,2})月\s*0?(\d{1,2})日")
TRAIL_RE = re.compile(r"\s*(?:政策|会議等|法令|採用)?\s*20\d{2}年\d{1,2}月\d{1,2}日$")

dbg  = lambda *m: print(*m, file=sys.stderr, flush=True) if DEBUG else None
sdbg = lambda *m: print("[SOU]", *m, file=sys.stderr, flush=True) if DEBUG_SOU else None

# ════════════════════════════════════════════════════════════════
#                         自 民 党
# ════════════════════════════════════════════════════════════════
HEAD_TAGS = ("dt", "h1", "h2", "h3", "h4", "li")
EXCLUDE_LDP = re.compile(r"^記者会見$")      # 除外ワード

def better(record_new, record_old):
    """どちらを残すか判定（本文がタイトルと同じなら劣る）"""
    body_n, body_o = record_new["body"], record_old["body"]
    ttl = record_new["title"]
    # 本文が空 or タイトルと同じ → 情報量 0
    score_n = len(body_n) if body_n and body_n != ttl else 0
    score_o = len(body_o) if body_o and body_o != ttl else 0
    return record_new if score_n > score_o else record_old

def scrape_ldp():
    # key=(日付, タイトル) で最良レコードを保持
    best = {}

    with sync_playwright() as p:
        ctx = (p.chromium
               .launch(headless=True,
                       args=["--disable-blink-features=AutomationControlled"])
               .new_context(user_agent=UA))
        page = ctx.new_page()

        for d in DATES:
            url = f"https://www.jimin.jp/activity/?day={d.year}.{d.month}.{d.day}"
            #dbg("[LDP] goto", url) <- デバックを見たければここを有効化
            try:
                page.goto(url, wait_until="networkidle", timeout=25_000)
            except Exception:
                continue
            soup = BeautifulSoup(page.content(), "html.parser")

            for tag in soup.find_all(HEAD_TAGS):
                ttl = tag.get_text(" ", strip=True)
                if not ttl or EXCLUDE_LDP.match(ttl):
                    continue
                if not kw_hit(ttl):
                    continue
                sib = tag.find_next_sibling() or tag
                body = sib.get_text(" ", strip=True)
                if body.startswith("今日の 自民党"):
                    body = ""

                rec = {
                    "date": f"{d.month}月{d.day}日",
                    "title": ttl,
                    "body": body.replace("Google Calenderに予定を追加", "").strip()
                }
                key = (rec["date"], rec["title"])
                if key in best:
                    best[key] = better(rec, best[key])
                else:
                    best[key] = rec
                #dbg(" 🔹LDP-HIT", ttl[:60])　<- デバックを見たければここを有効化

            time.sleep(WAIT_SEC)

    return list(best.values())

# ════════════════════════════════════════════════════════════════
def main():
    ldp = scrape_ldp()

    #print(f"\n===== {today.strftime('%-m月%-d日')} データ取得開始 =====\n")

    print("【自由民主党】")
    if ldp:
        # 文字列の日付を並び替えやすく整数化してソート
        def dt_key(r):
            m, d = map(int, r["date"].rstrip("日").split("月"))
            return (m, d)
        for r in sorted(ldp, key=dt_key, reverse=False):
            print(f"○{r['date']}　{r['title']}")
            if r['body'] and r['body'] != r['title']:
                print(f"　{r['body']}\n")
            else:
                print()          # 本文が空またはタイトルと同じなら 1 行で
    else:
        print("DXやデジタル化に関連する新着情報および審議会等の開催はいずれもなし\n")

# ───────────────────────────────────────────
if __name__ == "__main__":
    main()

#デジタル庁
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
digital_watcher.py  rev-4.6-DIGITAL  (2025-06-23)

■ 役割
  デジタル庁サイトの「プレスリリース」「ニュース」から、
  デジタル政策関連キーワードを含み、かつ一定期間 (LOOKBACK/AHEAD) 内に発信
  された記事を抽出して一覧表示する。
"""
import re
import time
import unicodedata
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ───────── 基本設定
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")
JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)

LOOKBACK = 5      # 今日 + 過去4日
AHEAD    = 7      # 未来 (開催案内など)
DIG_PAGES = 15    # 各カテゴリで深掘りするページ数
WAIT      = 0.3   # 秒

# ───────── キーワード定義
RAW_KW = [
    # 技術・行政一般
    "デジタル","情報通信","サイバー","AI","DX","ＤＸ","IT","SNS",
    "標準仕様","ガイドライン","無線局","免許状","光ファイバ",
]
SHORT = {"ai", "it", "dx"}

# 全角数字→半角化して NFKC 正規化、lower 化
half = lambda s: ''.join(chr(ord(c)-0xFEE0) if '０' <= c <= '９' else c for c in s)
norm_kw = [half(unicodedata.normalize("NFKC", k)).lower() for k in RAW_KW]

def kw_hit(txt: str) -> bool:
    t = half(unicodedata.normalize("NFKC", txt)).lower()
    return any(
        # 短いキーワードは単語境界でマッチング
        (re.search(rf"(?:^|[^a-z0-9]){k}(?:[^a-z0-9]|$)", t) if k in SHORT else k in t)
        for k in norm_kw
    )

# ───────── 日付判定
WIN_FROM = TODAY - timedelta(days=LOOKBACK - 1)
WIN_TO   = TODAY + timedelta(days=AHEAD)
in_window = lambda d: WIN_FROM <= d <= WIN_TO

DIG_ROOT = ["https://www.digital.go.jp/press", "https://www.digital.go.jp/news"]
dt_re = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

def article_date(html: str):
    """記事詳細 HTML から <time> または本文内の日付を取得"""
    soup = BeautifulSoup(html, "html.parser")
    # <time datetime="YYYY-MM-DD">
    if t := soup.find("time", datetime=True):
        y, m, d = map(int, t["datetime"][:10].split("-"))
        return datetime(y, m, d, tzinfo=JST)
    # 本文中の「YYYY年M月D日」
    if m := dt_re.search(soup.text):
        return datetime(*map(int, m.groups()), tzinfo=JST)

def scrape_digital():
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    hits, seen = [], set()

    for root in DIG_ROOT:
        for pg in range(1, DIG_PAGES + 1):
            url = root if pg == 1 else f"{root}?page={pg}"
            resp = sess.get(url, timeout=20)
            soup = BeautifulSoup(resp.text, "html.parser")
            page_has_hit = False

            for a in soup.select("a[href^='/press/'], a[href^='/news/']"):
                # タイトル取得
                title = a.get_text(" ", strip=True)
                # 末尾の「分類 ＋ YYYY年M月D日」を削除
                title = re.sub(r'\s+\S+\s+\d{4}年\d{1,2}月\d{1,2}日$', '', title)

                if not title or not kw_hit(title):
                    continue

                link = urljoin(url, a["href"])
                if link in seen:
                    continue

                # 記事ページを取得して日付を判定
                art_html = sess.get(link, timeout=20).text
                dt = article_date(art_html)
                if not dt or not in_window(dt):
                    continue

                hits.append({
                    "date": dt.strftime("%-m月%-d日"),
                    "title": title,
                    "url": link
                })
                seen.add(link)
                page_has_hit = True

            # ヒットが無いページでループ終了
            if not page_has_hit:
                break

            time.sleep(WAIT)

    return hits

def main():
    #print(f"===== Digital庁 Policy Watch ({TODAY:%-m/%-d}) =====\n")
    print("【デジタル庁】")
    results = scrape_digital()
    if not results:
        print("DXやデジタル化に関連する新着情報および審議会等の開催はいずれもなし\n")
        return
    for r in results:
        print(f"⚪︎{r['date']}　{r['title']}\n{r['url']}\n")

if __name__ == "__main__":
    main()


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
soumu_watcher.py  rev‑4.6‑SOU  (2025‑06‑12)

■ 役割
  総務省サイト「What's New」インデックスを走査し、
  デジタル・情報通信政策に関する告知のうち、LOOKBACK〜AHEAD 期間に
  該当するものを抽出して一覧表示する。
"""
import re, unicodedata
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ───────── 基本設定
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")
JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)

LOOKBACK = 5      # 今日 + 過去4日
AHEAD    = 7      # 未来 (開催案内など)

# ───────── キーワード定義
RAW_KW = [
    # 技術・行政一般
    "デジタル","情報通信","サイバー","AI","DX","ＤＸ","IT","SNS",
    "無線局","免許状","光ファイバ","標準仕様","ガイドライン",
    # 審議会関連
    "情報通信審議会","郵政政策部会","電気通信事業部会",
    "技術分科会","陸上無線通信委員会","IPネットワーク設備委員会",
]
SHORT = {"ai", "it", "dx"}

half = lambda s: ''.join(chr(ord(c)-0xFEE0) if '０' <= c <= '９' else c for c in s)
norm_kw = [half(unicodedata.normalize("NFKC", k)).lower() for k in RAW_KW]

def kw_hit(txt: str) -> bool:
    t = half(unicodedata.normalize("NFKC", txt)).lower()
    return any(
        re.search(rf"(?:^|[^a-z0-9]){k}(?:[^a-z0-9]|$)", t) if k in SHORT else k in t
        for k in norm_kw
    )

# ───────── 日付解析
jp_re  = re.compile(r"令和(\d+)年(\d{1,2})月(\d{1,2})日")
ymd_re = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
slash  = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")

def parse_dt(text: str):
    """タイトルまたは本文内から日付を抽出して datetime に変換"""
    t = half(unicodedata.normalize("NFKC", text))
    if m := jp_re.search(t):
        return datetime(2018 + int(m[1]), int(m[2]), int(m[3]), tzinfo=JST)
    if m := ymd_re.search(t):
        return datetime(*map(int, m.groups()), tzinfo=JST)
    if m := slash.search(t):
        return datetime(*map(int, m.groups()), tzinfo=JST)

WIN_FROM = TODAY - timedelta(days=LOOKBACK - 1)
WIN_TO   = TODAY + timedelta(days=AHEAD)
in_window = lambda d: WIN_FROM <= d <= WIN_TO

# ───────── 低レベル fetch（エンコーディング自動判定）
def fetch(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    enc = r.apparent_encoding or "utf-8"
    if enc.lower() == "utf-8" and b"\x82" in r.content[:300]:   # SJIS誤判定対策
        enc = "shift_jis"
    return r.content.decode(enc, "replace")

# ───────── What's New インデックス候補抽出
def list_candidates():
    idx = "https://www.soumu.go.jp/menu_kyotsuu/whatsnew/index.html"
    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_context().new_page()
        page.goto(idx, wait_until="networkidle", timeout=30000)
        soup = BeautifulSoup(page.content(), "html.parser")

    links = []
    for a in soup.find_all("a"):
        ttl = a.get_text(" ", strip=True)
        if ttl and kw_hit(ttl):
            links.append({"title": ttl, "url": urljoin(idx, a["href"])})
    return links

# ───────── 総務省スクレイプ
def scrape_soumu():
    results = []
    for rec in list_candidates():
        try:
            html = fetch(rec["url"])
        except Exception:
            continue
        dt = parse_dt(rec["title"]) or parse_dt(html)
        if not dt or not in_window(dt):
            continue
        results.append({"date": dt.strftime("%-m月%-d日"), **rec})

    # 重複排除
    uniq, filtered = set(), []
    for r in results:
        key = (r["date"], r["title"])
        if key in uniq:
            continue
        uniq.add(key)
        filtered.append(r)
    return filtered

# ───────── エントリポイント
def main():
    #print(f"===== 総務省 What's New Watch ({TODAY:%-m/%-d}) =====\n")
    print("【総務省】")
    results = scrape_soumu()
    if not results:
        print("DXやデジタル化に関連する新着情報および審議会等の開催はいずれもなし")
        return
    for r in results:
        print(f"○{r['date']}　{r['title']}\n　{r['url']}\n")


if __name__ == "__main__":
    main()

print("【経済産業省】")
print("自動化できないので手動で調べてください!!!!\n")

#内閣府    
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cao_press_watcher_rss_etree.py  rev-1.6  (2025-06-19)

■ 内閣府「報道発表新着情報」RSSフィードを標準ライブラリだけで取得・解析し、
  過去 4 日間に掲載された “DX／デジタル関連＋食品・環境” の
  リリースを抽出して一覧表示します。

・requests で RSS(XML) を取得
・xml.etree.ElementTree でパース
・email.utils.parsedate_to_datetime + datetime.fromisoformat で日付変換
依存:
    pip install requests
"""

import re
import unicodedata
import requests

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime

# ───────── Settings ──────────────────────────────────────
RSS_URL       = "https://www.cao.go.jp/rss/news.rdf"
LOOKBACK_DAYS = 4

# ───────── Date window ───────────────────────────────────
JST      = timezone(timedelta(hours=9))
NOW      = datetime.now(JST)
TODAY    = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
WIN_FROM = TODAY - timedelta(days=LOOKBACK_DAYS)

# ───────── Keywords ─────────────────────────────────────
KEYWORDS = [
    "環境",
    "DX", "デジタル", "クラウド", "ガバメントクラウド", "データセンター",
    "経済安全保障", "QUAD", "サプライチェーン", "セキュリティクリアランス",
    "電気通信事業法", "サイバーセキュリティ", "Web3", "半導体", "AI",
    "GIGAスクール構想", "量子コンピューター", "スーパーコンピュータ",
    "スマホ新法", "青少年インターネット環境整備法", "Fintech",
    "中央銀行デジタル通貨", "知的財産", "個人情報保護", "医療DX",
    "新年度予算（デジタル関連）"
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

# ───────── Fetch RSS ─────────────────────────────────────
def fetch_rss(url: str) -> str:
    resp = requests.get(url, timeout=(10, 30))
    resp.raise_for_status()
    return resp.text

# ───────── Parse and filter ─────────────────────────────
def scrape_cao_rss():
    xml = fetch_rss(RSS_URL)
    root = ET.fromstring(xml)

    # define namespaces
    ns = {
        'rss': 'http://purl.org/rss/1.0/',
        'dc':  'http://purl.org/dc/elements/1.1/'
    }

    items = root.findall('rss:item', ns)

    results = []
    for itm in items:
        title_el = itm.find('rss:title', ns)
        link_el  = itm.find('rss:link',  ns)
        date_el  = itm.find('dc:date',   ns)
        if title_el is None or link_el is None or date_el is None:
            continue

        title = title_el.text.strip()
        link  = link_el.text.strip()
        date_text = date_el.text.strip()

        # parse RFC822 or ISO8601 date
        dt = None
        try:
            dt = parsedate_to_datetime(date_text)
        except Exception:
            try:
                dt = datetime.fromisoformat(date_text)
            except Exception:
                continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        dt_jst = dt.astimezone(JST)
        dt0    = dt_jst.replace(hour=0, minute=0, second=0, microsecond=0)

        # date window filter
        if not (WIN_FROM <= dt0 <= TODAY):
            continue

        # keyword filter
        if not kw_hit(title):
            continue

        results.append({
            'dt':   dt0,
            'date': dt0.strftime('%-m月%-d日'),
            'title': title,
            'url':   link
        })

    # dedupe & sort descending
    seen = set()
    out = []
    for r in sorted(results, key=lambda x: x['dt'], reverse=True):
        key = (r['date'], r['title'])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    return out

# ───────── CLI ─────────────────────────────────────────
def main():
    recs = scrape_cao_rss()
    print("【内閣府】")
    if not recs:
        print("DXやデジタル化に関連する新着情報および審議会等の開催はいずれもなし\n")
        return
    for r in recs:
        print(f"○{r['date']}　{r['title']}\n")
        print(f"　{r['url']}\n")

if __name__ == "__main__":
    main()

#ーーーーーーーーNISCーーーーーーーーーーーーー
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import unicodedata
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

def to_ascii(s: str) -> str:
    """
    全角数字などを半角に正規化するヘルパー
    """
    return unicodedata.normalize('NFKC', s)

def fetch_recent_nisc_news(days: int = 4):
    BASE_URL = 'https://www.nisc.go.jp'
    # 抽出対象とするキーワード
    KEYWORDS = [
        "デジタル", "情報通信", "サイバー", "AI", "DX", "ＤＸ", "IT", "SNS",
        "標準仕様", "ガイドライン", "無線局", "免許状", "光ファイバ"
    ]

    today     = datetime.now()
    threshold = today - timedelta(days=days)
    results   = []

    # —— デバッグ出力 ——
    #print(f'DEBUG: today     = {today.strftime("%Y-%m-%d")}')
    #print(f'DEBUG: threshold = {threshold.strftime("%Y-%m-%d")}')

    # 閾値〜今日までの各日付ページをチェック
    for delta in range((today - threshold).days + 1):
        dt  = threshold + timedelta(days=delta)
        url = f'{BASE_URL}/news/{dt.strftime("%Y%m%d")}.html'
        #print(f'DEBUG: checking URL = {url}')

        resp = requests.get(url)
        #print(f'DEBUG: raw status_code = {resp.status_code}')
        if resp.status_code != 200:
            continue

        # 1) バイト列でパースして<meta charset>を探す
        soup_bytes = BeautifulSoup(resp.content, 'html.parser')
        meta_charset = soup_bytes.find('meta', attrs={'charset': True})
        if meta_charset:
            encoding = meta_charset['charset']
        else:
            encoding = resp.encoding or resp.apparent_encoding or 'utf-8'
        #print(f'DEBUG: detected encoding = {encoding}')

        # 2) 正しいエンコーディングでテキストに変換し直し
        resp.encoding = encoding
        page_text = resp.text
        soup      = BeautifulSoup(page_text, 'html.parser')

        # 3) キーワードフィルタ
        full_text = soup.get_text()
        matched = any(kw in full_text for kw in KEYWORDS)
        #print(f'DEBUG: keyword match = {matched}')
        if not matched:
            continue

        # 4) タイトル取得
        h2 = soup.find('h2')
        title = h2.get_text(strip=True) if h2 else '[タイトル不明]'
        #print(f'DEBUG: title = {title}')

        # 5) ページ内から公開日をパース
        raw_date = ''
        elem = soup.find(string=re.compile(r'\d{4}年'))
        if elem:
            raw_date = elem.strip()
        #print(f'DEBUG: raw date_text = "{raw_date}"')

        ascii_date = to_ascii(raw_date)
        m = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', ascii_date)
        if m:
            y, mth, d = map(int, m.groups())
            dt_pub = datetime(y, mth, d)
            #print(f'DEBUG: parsed dt_pub = {dt_pub.strftime("%Y-%m-%d")}')
        else:
            # うまくパースできなければ URL 日付をそのまま使う
            dt_pub = dt
            #print(f'DEBUG: date parse failed, using URL date = {dt_pub.strftime("%Y-%m-%d")}')

        results.append((dt_pub, title, url))

    #print(f'DEBUG: total matched results = {len(results)}\n')

    # —— 最終出力 ——
    print('【内閣サイバーセキュリティセンター・NISC】')
    if results:
        for dt_pub, title, url in sorted(results):
            print(f'○{dt_pub.month}月{dt_pub.day}日　「{title}」')
            print(f'　{url}\n')
    else:
        print(f'{threshold.month}月{threshold.day}日〜{today.month}月{today.day}日　'
              'DXやデジタル化に関連する新着情報および審議会等の開催はいずれもなし\n')


if __name__ == '__main__':
    fetch_recent_nisc_news(4)

#-----------金融庁ーーーーーーーーーーーー
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import unicodedata
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

def to_ascii(s: str) -> str:
    """
    全角数字などを半角に正規化
    """
    return unicodedata.normalize('NFKC', s)

def fetch_fsa_news(days: int = 4):
    BASE_URL = 'https://www.fsa.go.jp'
    # 抽出対象とするキーワード（人事・人事異動も追加）
    KEYWORDS = [
        "デジタル", "情報通信", "サイバー", "AI", "DX", "ＤＸ", "IT", "SNS",
        "標準仕様", "ガイドライン", "無線局", "免許状", "光ファイバ",
        "人事", "人事異動"
    ]

    today     = datetime.now()
    threshold = today - timedelta(days=days)
    results   = []

    # —— デバッグ出力 ——
    #print(f'DEBUG: today     = {today.strftime("%Y-%m-%d")}')
    #print(f'DEBUG: threshold = {threshold.strftime("%Y-%m-%d")}')

    # ① /inter/etc/YYYYMMDD/YYYYMMDD.html のループチェック
    for delta in range((today - threshold).days + 1):
        dt = threshold + timedelta(days=delta)
        subpath = f'/inter/etc/{dt.strftime("%Y%m%d")}/{dt.strftime("%Y%m%d")}.html'
        url     = BASE_URL + subpath
        #print(f'DEBUG: checking URL = {url}')

        resp = requests.get(url)
        #print(f'DEBUG: status_code = {resp.status_code}')
        if resp.status_code != 200:
            continue

        # エンコーディング検出＆再デコード
        soup_bytes   = BeautifulSoup(resp.content, 'html.parser')
        meta_charset = soup_bytes.find('meta', attrs={'charset': True})
        encoding = (meta_charset['charset']
                    if meta_charset
                    else resp.encoding or resp.apparent_encoding or 'utf-8')
        #print(f'DEBUG: detected encoding = {encoding}')
        resp.encoding = encoding
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 公開日（令和表記）を本文からパース
        raw_date = ''
        date_elem = soup.find(string=re.compile(r'令和'))
        if date_elem:
            raw_date = date_elem.strip()
        #print(f'DEBUG: raw_date = "{raw_date}"')

        m = re.search(r'令和(\d+)年\s*(\d+)月\s*(\d+)日',
                      to_ascii(raw_date))
        if m:
            era, mth, d = map(int, m.groups())
            year = 2018 + era
            dt_pub = datetime(year, mth, d)
        else:
            dt_pub = dt
        #print(f'DEBUG: parsed dt_pub = {dt_pub.strftime("%Y-%m-%d")}')

        # タイトル取得
        title_tag = soup.find('title') or soup.find('h1') or soup.find('h2')
        title     = title_tag.get_text(strip=True) if title_tag else '[タイトル不明]'
        #print(f'DEBUG: title = {title}')

        # キーワードフィルタ（本文全体）
        full_text = soup.get_text()
        matched   = any(kw in full_text for kw in KEYWORDS)
        #print(f'DEBUG: keyword match = {matched}')
        if not matched:
            continue

        results.append((dt_pub, title, url))

    # ② 人事異動ページ （キーワードフィルタを適用）
    j_url = BASE_URL + '/common/about/jinji/index.html'
    #print(f'DEBUG: checking HR URL = {j_url}')
    resp = requests.get(j_url)
    #print(f'DEBUG: status_code = {resp.status_code}')
    if resp.status_code == 200:
        soup_bytes   = BeautifulSoup(resp.content, 'html.parser')
        meta_charset = soup_bytes.find('meta', attrs={'charset': True})
        encoding = (meta_charset['charset']
                    if meta_charset
                    else resp.encoding or resp.apparent_encoding or 'utf-8')
        #print(f'DEBUG: detected encoding HR = {encoding}')
        resp.encoding = encoding
        soup = BeautifulSoup(resp.text, 'html.parser')

        full_text = soup.get_text()
        matched   = any(kw in full_text for kw in KEYWORDS)
        #print(f'DEBUG: HR keyword match = {matched}')
        if matched:
            # 発令日をすべて抽出＆範囲チェック
            text_ascii = to_ascii(full_text)
            hr_dates = re.findall(r'令和7年\s*(\d+)月\s*(\d+)日発令', text_ascii)
            #print(f'DEBUG: HR raw matches = {hr_dates}')
            for mth_str, day_str in hr_dates:
                mth, day = int(mth_str), int(day_str)
                dt_pub = datetime(2018 + 7, mth, day)
                in_range = threshold <= dt_pub <= today
                #print(f'DEBUG: HR dt_pub = {dt_pub.strftime("%Y-%m-%d")}, in_range = {in_range}')
                if in_range:
                    title = f'人事異動（令和７年{mth}月{day}日付）について公表しました。'
                    results.append((dt_pub, title, j_url))

    #print(f'DEBUG: total matched results = {len(results)}\n')

    # —— 最終出力 ——
    print('【金融庁】')
    if results:
        for dt_pub, title, url in sorted(results):
            print(f'○{dt_pub.month}月{dt_pub.day}日　「{title}」')
            print(f'　{url}\n')
    else:
        print(f'{threshold.month}月{threshold.day}日〜{today.month}月{today.day}日　'
              'DXやデジタル化に関連する新着情報および審議会等の開催はいずれもなし\n')


if __name__ == '__main__':
    fetch_fsa_news(4)


#ニュース    
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gov_dx_news_scraper.py  rev-2.1  (2025-06-17)

■ 指定キーワードで Google News RSS を検索し、
  省庁・自治体が関与する DX 関連ニュースのみ抽出。
  4日より古い記事は除外します。
"""

# ───────── Imports ──────────────────────────────────────────
import re, sys, html, time, hashlib, unicodedata, requests, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

# ───────── 検索キーワード ────────────────────────────────
KEYWORDS = [
    "DX","デジタル","クラウド","ガバメントクラウド","データセンター",
    "経済安全保障","QUAD","サプライチェーン","セキュリティクリアランス",
    "電気通信事業法","サイバーセキュリティ","Web3","半導体","AI",
    "GIGAスクール構想","量子コンピューター","スーパーコンピュータ",
    "スマホ新法","青少年インターネット環境整備法","Fintech",
    "中央銀行デジタル通貨","知的財産","個人情報保護","医療DX",
    "新年度予算 デジタル","スマホソフトウェア競争促進法","apple"
    "アップル","グーグル","google","相互運用性"
]

# ───────── 行政主体フィルタ ────────────────────────────
MINISTRIES = [
    "総務省","経済産業省","デジタル庁","文部科学省","経産省","厚生労働省",
    "農林水産省","国土交通省","財務省","金融庁","環境省","外務省","防衛省",
    "内閣府","内閣官房","警察庁","消防庁","復興庁","公正取引委員会","公取委",
    "国交省","厚労省","農水省","デジ庁","文科省","Google","アップル","apple",
]
PREF_SUFFIX = ("県","府","都","市","町","村")

# ───────── 検索設定 ────────────────────────────────────
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")
JST  = timezone(timedelta(hours=9))
SINCE_DAYS = 4  # ← ここを 4 日に変更
RSS_URL = "https://news.google.com/rss/search?hl=ja&gl=JP&ceid=JP:ja&q=" \
          "{}%20when:4d"  # ← when:14d を when:4d に変更

# ───────── ユーティリティ ──────────────────────────────
def is_gov_related(text:str)->bool:
    if any(w in text for w in MINISTRIES):
        return True
    if re.search(r"(政府|内閣|自治体|国が|国は)", text):
        return True
    for suf in PREF_SUFFIX:
        if re.search(rf"[^\w]{{1,4}}{suf}", text):
            return True
    return False

def strip_html(raw:str)->str:
    return BeautifulSoup(html.unescape(raw), "html.parser").get_text(" ", strip=True)

# ───────── RSS 取得 & 解析 ────────────────────────────
def fetch_hits(keyword:str):
    url = RSS_URL.format(quote_plus(keyword))
    headers = {"User-Agent": UA}
    xml_data = requests.get(url, headers=headers, timeout=30).content

    root = ET.fromstring(xml_data)
    for item in root.iterfind(".//item"):
        title = strip_html(item.findtext("title", default=""))
        descr = strip_html(item.findtext("description", default=""))
        if not is_gov_related(title + descr):
            continue

        link = item.findtext("link", default="")
        try:
            dt = parsedate_to_datetime(item.findtext("pubDate", ""))
        except Exception:
            continue
        dt = dt.astimezone(JST)
        if dt < datetime.now(JST) - timedelta(days=SINCE_DAYS):
            continue

        yield {
            "dt": dt,
            "date": f"{dt.month}月{dt.day}日",
            "title": title,
            "url": link
        }

# ───────── メイン ──────────────────────────────────────
def main():
    news, seen = [], set()
    for kw in KEYWORDS:
        try:
            for hit in fetch_hits(kw):
                uid = hashlib.md5(hit["url"].encode()).hexdigest()
                if uid in seen:
                    continue
                seen.add(uid)
                news.append(hit)
        except Exception as e:
            print(f"[WARN] {kw}: {e}", file=sys.stderr)
        time.sleep(0.6)

    news.sort(key=lambda x: x["dt"])

    print("【ニュース】")
    if not news:
        print("該当記事なし")
        return
    for n in news:
    # ・6月9日　タイトル
        print(f"○{n['date']}　{n['title']}")
    # 　URL
        print(f"　{n['url']}\n")


if __name__ == "__main__":
    main()
