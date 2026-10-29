"""
뉴스 수집 모듈
- 국내: 네이버 증권 '종목 뉴스'(종목코드로 분류된 뉴스, MTS 뉴스 탭과 같은 성격)
- 해외: Google 뉴스(영문) — 시가총액 상위 종목만
- 받아 온 기사는 cache/news/ 에 쌓아 두고, 매번 '지금' 기준으로 48시간·7일 개수를 다시 셉니다.
  그래서 수집이 막히는 날에도 이전 기사로 화면이 유지됩니다.
- 네이버 내부 주소는 공식 API가 아니라 바뀔 수 있으므로 여러 주소를 차례로 시험해 되는 것을 씁니다.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
PAGE_SIZE = 20
MAX_PAGES = 6            # 종목당 최대 120건까지 (넘으면 '120+'로 표시)
KEEP_DAYS = 8            # 캐시에 남겨 둘 기간
WORKERS = 4
DELAY = 0.15             # 요청 사이 쉬는 시간(초)
GLOBAL_TOP = 150         # 해외 뉴스를 셀 시가총액 상위 종목 수

ENDPOINTS = [
    ("m-api-news-stock", "https://m.stock.naver.com/api/news/stock/{code}?pageSize={size}&page={page}"),
    ("m-domestic-detail", "https://m.stock.naver.com/api/domestic/detail/news?itemCode={code}&page={page}&pageSize={size}"),
    ("stock-domestic-detail", "https://stock.naver.com/api/domestic/detail/news?itemCode={code}&page={page}&pageSize={size}"),
    ("pc-html", "https://finance.naver.com/item/news_news.naver?code={code}&page={page}&sm=title_entity_id.basic&clusterId="),
]


def now_kst():
    return datetime.now(KST)


def log(*a):
    print(now_kst().strftime("%H:%M:%S"), "[뉴스]", *a, flush=True)


# ---------------------------------------------------------------- 날짜·응답 해석
def parse_dt(v):
    """여러 형식의 날짜를 KST datetime으로"""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            x = float(v)
            if x > 1e12:
                x /= 1000
            if x > 1e9:
                return datetime.fromtimestamp(x, KST)
            return None
        s = str(v).strip()
        if not s:
            return None
        if re.fullmatch(r"\d{12}|\d{14}", s):
            return datetime.strptime(s[:12], "%Y%m%d%H%M").replace(tzinfo=KST)
        if re.fullmatch(r"\d{13}|\d{10}", s):
            return parse_dt(int(s))
        m = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})[ T]*(\d{1,2})?:?(\d{2})?", s)
        if m:
            y, mo, d, hh, mm = m.groups()
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
            if re.search(r"(Z|[+\-]\d{2}:?\d{2})$", s):
                try:
                    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST)
                except ValueError:
                    pass
            return dt.replace(tzinfo=KST)
        return parsedate_to_datetime(s).astimezone(KST)
    except Exception:
        return None


TITLE_KEYS = ("title", "tit", "articleTitle", "headline")
DATE_KEYS = ("datetime", "dateTime", "date", "wdate", "pubDate", "createdAt", "publishedAt",
             "regDate", "time", "articleDate")
SRC_KEYS = ("officeName", "press", "pressName", "source", "officeHname", "provider")
LINK_KEYS = ("mobileNewsUrl", "linkUrl", "link", "url", "originalLink", "originallink", "newsUrl")


def _clean(s):
    s = re.sub(r"<[^>]+>", "", str(s))
    return (s.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&#39;", "'").replace("&apos;", "'").strip())


def items_from_json(obj):
    """응답 JSON 어디에 있든 '제목+날짜'가 있는 항목을 찾아 냅니다."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            t = next((x[k] for k in TITLE_KEYS if isinstance(x.get(k), str) and x.get(k)), None)
            dv = next((x[k] for k in DATE_KEYS if x.get(k) not in (None, "")), None)
            if t and dv is not None:
                dt = parse_dt(dv)
                if dt:
                    link = next((x[k] for k in LINK_KEYS if isinstance(x.get(k), str) and x[k].startswith("http")), "")
                    oid, aid = x.get("officeId"), x.get("articleId")
                    if oid and aid:
                        link = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
                    src = next((x[k] for k in SRC_KEYS if isinstance(x.get(k), str) and x.get(k)), "")
                    out.append({"t": _clean(t), "s": _clean(src), "d": dt, "u": link})
                    return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return out


def items_from_html(text):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "html.parser")
    out = []
    for tr in soup.select("tr"):
        a = tr.select_one("td.title a")
        dtd = tr.select_one("td.date")
        if not a or not dtd:
            continue
        dt = parse_dt(dtd.get_text(" ", strip=True))
        if not dt:
            continue
        href = a.get("href", "")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if q.get("office_id") and q.get("article_id"):
            link = f"https://n.news.naver.com/mnews/article/{q['office_id'][0]}/{q['article_id'][0]}"
        else:
            link = urllib.parse.urljoin("https://finance.naver.com", href)
        info = tr.select_one("td.info")
        out.append({"t": _clean(a.get_text(" ", strip=True)), "s": info.get_text(strip=True) if info else "",
                    "d": dt, "u": link})
    return out


# ---------------------------------------------------------------- HTTP
class Blocked(Exception):
    pass


class Client:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        self.lock = threading.Lock()
        self.blocked_hits = 0
        self.fail_streak = 0

    def get(self, url, referer=None):
        h = {"Referer": referer} if referer else {}
        r = self.s.get(url, headers=h, timeout=12)
        time.sleep(DELAY)
        if r.status_code in (401, 403, 429):
            with self.lock:
                self.blocked_hits += 1
                if self.blocked_hits >= 5:
                    raise Blocked(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r


def fetch_page(client, ep, code, page):
    name, tpl = ep
    url = tpl.format(code=code, page=page, size=PAGE_SIZE)
    if name == "pc-html":
        r = client.get(url, referer=f"https://finance.naver.com/item/news.naver?code={code}")
        r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "euc-kr"
        return items_from_html(r.text)
    r = client.get(url, referer="https://m.stock.naver.com/")
    return items_from_json(r.json())


def detect_endpoint(client, probe="005930"):
    for ep in ENDPOINTS:
        try:
            items = fetch_page(client, ep, probe, 1)
            recent = [i for i in items if i["d"] > now_kst() - timedelta(days=30)]
            log(f"주소 시험 {ep[0]}: 기사 {len(items)}건 (최근 30일 {len(recent)}건)")
            if len(recent) >= 3:
                return ep
        except Blocked:
            raise
        except Exception as e:
            log(f"주소 시험 {ep[0]} 실패: {type(e).__name__} {str(e)[:120]}")
    return None


def fetch_stock_news(client, ep, code):
    cutoff = now_kst() - timedelta(days=7)
    items, capped, seen = [], False, set()
    for page in range(1, MAX_PAGES + 1):
        got = fetch_page(client, ep, code, page)
        fresh = [i for i in got if (i["t"], i["d"]) not in seen]
        if not fresh:            # 빈 페이지이거나 같은 페이지가 반복되면 끝
            break
        seen.update((i["t"], i["d"]) for i in fresh)
        items += fresh
        got = fresh
        if min(i["d"] for i in got) < cutoff:
            break
        if page == MAX_PAGES:
            capped = True
    return items, capped


# ---------------------------------------------------------------- 해외(영문) 뉴스
ENG_SUFFIX = re.compile(r"[,\s]*(co\.?,?\s*ltd\.?|company|corporation|corp\.?|inc\.?|ltd\.?|limited|holdings?)\s*$", re.I)


def english_name(client, code, market):
    for url in (f"https://m.stock.naver.com/api/stock/{code}/basic",):
        try:
            j = client.get(url, referer="https://m.stock.naver.com/").json()
            n = j.get("stockNameEng") or j.get("engName") or ""
            if n:
                return n
        except Blocked:
            raise
        except Exception:
            pass
    suf = "KS" if market == "KOSPI" else "KQ"
    try:
        j = client.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={code}.{suf}&quotesCount=1&newsCount=0").json()
        q = (j.get("quotes") or [{}])[0]
        return q.get("longname") or q.get("shortname") or ""
    except Blocked:
        raise
    except Exception:
        return ""


def clean_eng(n):
    n = re.sub(r"\s+", " ", n or "").strip()
    for _ in range(2):
        n = ENG_SUFFIX.sub("", n).strip()
    return n if len(n) >= 4 else ""


def fetch_global(client, eng):
    q = urllib.parse.quote(f'"{eng}" when:7d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    r = client.get(url)
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        t = it.findtext("title") or ""
        src = it.findtext("source") or ""
        if src and t.endswith(" - " + src):
            t = t[: -len(src) - 3]
        dt = parse_dt(it.findtext("pubDate"))
        if dt:
            out.append({"t": _clean(t), "s": src, "d": dt, "u": it.findtext("link") or "", "g": 1})
    return out


# ---------------------------------------------------------------- 저장·집계
def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def merge_items(old, new):
    seen, out = set(), []
    for i in sorted(new + old, key=lambda x: x["d"], reverse=True):
        key = (re.sub(r"\W", "", i["t"])[:40], i.get("g", 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    lim = now_kst() - timedelta(days=KEEP_DAYS)
    return [i for i in out if i["d"] >= lim]


def to_disk(items):
    return [{**i, "d": i["d"].strftime("%Y-%m-%dT%H:%M")} for i in items]


def from_disk(items):
    out = []
    for i in items or []:
        d = parse_dt(i.get("d"))
        if d:
            out.append({**i, "d": d})
    return out


def count(items, glob=0):
    now = now_kst()
    sel = [i for i in items if i.get("g", 0) == glob]
    n48 = sum(1 for i in sel if i["d"] >= now - timedelta(hours=48))
    n7 = sum(1 for i in sel if i["d"] >= now - timedelta(days=7))
    return n48, n7


def collect(snapshot, cache_dir, min_amount=5.0, max_stocks=1500):
    """snapshot: code, market, amount, marcap 열이 있는 DataFrame.
    반환: (종목별 {"items":[...], "capped":bool} dict, 상태 dict)"""
    ndir = os.path.join(cache_dir, "news")
    os.makedirs(ndir, exist_ok=True)
    uni = snapshot[(snapshot["amount"] >= min_amount) & (snapshot["code"].str[-1] == "0")]
    uni = uni.sort_values("amount", ascending=False).head(max_stocks)
    codes = list(uni["code"])
    status = {"domestic": "ok", "global": "ok", "endpoint": None, "universe": len(codes), "fetched": 0, "failed": 0}
    client = Client()
    results = {}

    # 국내
    try:
        ep = detect_endpoint(client)
        if ep is None:
            status["domestic"] = "no-endpoint"
            log("사용할 수 있는 뉴스 주소를 찾지 못했습니다. 이전에 받아 둔 기사로 계산합니다.")
        else:
            status["endpoint"] = ep[0]
            log(f"'{ep[0]}' 주소로 {len(codes)}종목 수집 시작")
            fails = 0
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(fetch_stock_news, client, ep, c): c for c in codes}
                for n, fut in enumerate(as_completed(futs), 1):
                    c = futs[fut]
                    try:
                        items, capped = fut.result()
                        results[c] = {"items": items, "capped": capped}
                        status["fetched"] += 1
                    except Blocked as e:
                        status["domestic"] = "blocked"
                        log("요청이 차단되었습니다:", e, "— 수집을 멈추고 이전 기사를 사용합니다.")
                        for f in futs:
                            f.cancel()
                        break
                    except Exception:
                        fails += 1
                        status["failed"] += 1
                    if n % 200 == 0:
                        log(f"  {n}/{len(codes)}")
    except Blocked as e:
        status["domestic"] = "blocked"
        log("요청이 차단되었습니다:", e)

    # 해외 (시가총액 상위)
    if status["domestic"] == "blocked":
        status["global"] = "skipped"
        status["eng_names"] = {k: v for k, v in (_load(os.path.join(cache_dir, "eng_names.json")) or {}).items() if v}
    else:
        names_path = os.path.join(cache_dir, "eng_names.json")
        names = _load(names_path) or {}
        top = snapshot.sort_values("marcap", ascending=False).head(GLOBAL_TOP)
        g_ok = 0
        try:
            for rec in top.itertuples(index=False):
                if rec.code not in names:
                    names[rec.code] = clean_eng(english_name(client, rec.code, rec.market))
                eng = names.get(rec.code)
                if not eng:
                    continue
                try:
                    g = fetch_global(client, eng)
                    results.setdefault(rec.code, {"items": [], "capped": False})["items"] += g
                    results[rec.code]["global_ok"] = True
                    g_ok += 1
                    time.sleep(0.8)
                except Blocked:
                    raise
                except Exception:
                    pass
        except Blocked:
            status["global"] = "blocked"
        status["global_fetched"] = g_ok
        with open(names_path, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False)
        status["eng_names"] = {k: v for k, v in names.items() if v}

    # 캐시와 합치기
    final = {}
    in_snap = set(snapshot["code"])
    cached = {f[:-5] for f in os.listdir(ndir) if f.endswith(".json")} & in_snap
    for c in set(codes) | set(results) | cached:
        path = os.path.join(ndir, f"{c}.json")
        old = _load(path) or {}
        old_items = from_disk(old.get("items"))
        new = results.get(c)
        if new is not None:
            items = merge_items(old_items, new["items"])   # 예전에 받은 기사 + 새 기사 (중복 제거)
            capped = new.get("capped", False)
            fetched_at = now_kst().strftime("%Y-%m-%dT%H:%M")
        else:
            items = merge_items(old_items, [])
            capped = old.get("capped", False)
            fetched_at = old.get("fetched_at")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": to_disk(items), "capped": capped, "fetched_at": fetched_at}, f, ensure_ascii=False)
        final[c] = {"items": items, "capped": capped, "fetched_at": fetched_at}
    log(f"완료: 국내 {status['domestic']} ({status['fetched']}종목), 해외 {status['global']} ({status.get('global_fetched', 0)}종목)")
    return final, status
