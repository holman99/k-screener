"""
업종 분류 + 업종 대비 등락률
- 분류: 네이버 증권 업종(약 80개, 예: 제약 / 반도체와반도체장비 / 건설)을 1순위로 씁니다.
  ① 네이버 증권 앱 API → ② 네이버 증권 PC 페이지 → ③ 거래소 KIND 표준산업분류 순으로 시도하고,
  소속부(우량기업부 등)처럼 업종이 아닌 값이 섞이면 버립니다. 모두 실패하면 지난번 분류를 씁니다.
- 업종 등락률: 같은 업종 종목들의 등락률을 시가총액으로 가중 평균하되, '자기 자신은 뺀' 값입니다.
  (작은 업종에서 자기 자신이 업종 수익률을 끌어올리는 착시를 막기 위해)
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
LIST_URL = "https://finance.naver.com/sise/sise_group.naver?type=upjong"
DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={no}"
MIN_GROUP = 3


def log(*a):
    print(datetime.now(KST).strftime("%H:%M:%S"), "[업종]", *a, flush=True)


def _get(s, url):
    r = s.get(url, timeout=15, headers={"Referer": "https://finance.naver.com/sise/"})
    r.raise_for_status()
    return r.content.decode("euc-kr", errors="replace")


def _snip(x):
    return re.sub(r"\s+", " ", str(x))[:200]


# ---------------------------------------------------------------- ① 네이버 증권 앱 API (업종 = 제약, 반도체와반도체장비, 건설 …)
API_HOSTS = ["https://m.stock.naver.com", "https://stock.naver.com"]
API_LIST = "/api/domestic/market/upjong/list?startIdx={start}&pageSize=100&sortType=changeRate"
API_MEMBERS = "/api/domestic/market/upjong/{no}/stocklist?marketType=ALL&orderType=quantTop&startIdx={start}&pageSize=100"
NO_KEYS = ("no", "upjongNo", "industryCode", "upjongCode", "groupNo", "id", "code")
NAME_KEYS = ("name", "upjongName", "industryName", "groupName", "upjongNm", "korName")
CODE_KEYS = ("itemCode", "stockCode", "code", "symbolCode", "reutersCode", "itemcode")


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def parse_api_groups(j):
    out, seen = [], set()
    for d in _walk(j):
        no = next((d[k] for k in NO_KEYS if isinstance(d.get(k), (int, str)) and str(d.get(k)).isdigit()), None)
        name = next((d[k] for k in NAME_KEYS if isinstance(d.get(k), str) and d.get(k).strip()), None)
        if no is not None and name and str(no) not in seen and not re.fullmatch(r"\d{6}", str(no)):
            seen.add(str(no))
            out.append((str(no), name.strip()))
    return out


def parse_api_members(j):
    codes = []
    for d in _walk(j):
        for k in CODE_KEYS:
            v = d.get(k)
            if isinstance(v, str):
                m = re.match(r"^A?(\d{6})(\.K[SQ])?$", v.strip())
                if m:
                    codes.append(m.group(1))
                    break
    return list(dict.fromkeys(codes))


def fetch_naver_api_map():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://m.stock.naver.com/"})
    last_err = None
    for host in API_HOSTS:
        try:
            groups = []
            for start in (0, 100):
                r = s.get(host + API_LIST.format(start=start), timeout=15)
                r.raise_for_status()
                g = parse_api_groups(r.json())
                groups += [x for x in g if x[0] not in {y[0] for y in groups}]
                if len(g) < 100:
                    break
            log(f"앱 API({host}) 업종 {len(groups)}개 예: {groups[:5]}")
            if len(groups) < 20:
                raise RuntimeError("업종 목록이 너무 적음: " + _snip(r.text))
            mp = {}
            for no, name in groups:
                for start in range(0, 1000, 100):
                    try:
                        r = s.get(host + API_MEMBERS.format(no=no, start=start), timeout=15)
                        r.raise_for_status()
                        codes = parse_api_members(r.json())
                    except Exception as e:
                        log(f"{name} 구성종목 실패: {e}")
                        break
                    for c in codes:
                        mp.setdefault(c, name)
                    time.sleep(0.15)
                    if len(codes) < 100:
                        break
            return mp
        except Exception as e:
            last_err = e
            log(f"앱 API({host}) 실패: {type(e).__name__} {str(e)[:200]}")
    raise RuntimeError(last_err)


# ---------------------------------------------------------------- ② 네이버 증권 PC 페이지
def fetch_naver_map():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    html = _get(s, LIST_URL)
    groups = parse_group_list(html)
    if len(groups) < 20:
        raise RuntimeError(f"업종 목록이 너무 적습니다({len(groups)}개): {_snip(html)}")
    mp = {}
    for no, name in groups:
        try:
            for code in parse_group_members(_get(s, DETAIL_URL.format(no=no))):
                mp.setdefault(code, name)
        except Exception as e:
            log(name, "실패:", e)
        time.sleep(0.3)
    return mp


# ---------------------------------------------------------------- ③ 한국거래소 KIND 상장법인 목록의 '업종' (표준산업분류)
KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"


def short_ksic(name):
    n = re.sub(r"\s+", " ", name).strip()
    n = re.sub(r"\s*(제조업|도매업|소매업|서비스업|개발 및 공급업|공급업)$", "", n)
    return n or name


def fetch_kind_map():
    from bs4 import BeautifulSoup
    r = requests.get(KIND_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    html = r.content.decode("euc-kr", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    head = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    ic, iu = head.index("종목코드"), head.index("업종")
    mp = {}
    for tr in rows[1:]:
        tds = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(tds) > max(ic, iu) and tds[iu]:
            mp[tds[ic].zfill(6)] = short_ksic(tds[iu])
    return mp


# ---------------------------------------------------------------- 검사·불러오기
def looks_like_industry(mp):
    """소속부(우량기업부 등)나 시장 구분이 섞이면 업종이 아닙니다."""
    if len(mp) < 500:
        return False, f"종목 수 부족({len(mp)})"
    vals = list(mp.values())
    bad = sum(1 for v in vals if re.search(r"(기업부|소속부|관리종목|KOSPI|KOSDAQ|코스닥|유가증권)", str(v)))
    if bad > len(vals) * 0.05:
        return False, f"업종이 아닌 값이 섞임(예: {next(v for v in vals if re.search('기업부|소속부', str(v)))})"
    if len(set(vals)) < 15:
        return False, f"업종 종류가 너무 적음({len(set(vals))}개)"
    return True, ""


SOURCES = (("naver-app", fetch_naver_api_map), ("naver-pc", fetch_naver_map), ("kind", fetch_kind_map))


def load_map(cache_dir, refresh=True):
    path = os.path.join(cache_dir, "sector_map_v2.json")
    old = {}
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        if not looks_like_industry(old.get("map", {}))[0]:
            old = {}
    except Exception:
        pass
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if old.get("date") == today or not refresh:
        return old.get("map", {}), old.get("source", "cache")
    for src, fn in SOURCES:
        try:
            mp = fn()
            ok, why = looks_like_industry(mp)
            if ok:
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"date": today, "source": src, "map": mp}, f, ensure_ascii=False)
                log(f"{src} 분류 {len(mp)}종목, 업종 {len(set(mp.values()))}개")
                return mp, src
            log(f"{src} 분류 사용 안 함: {why}")
        except Exception as e:
            log(f"{src} 분류 실패: {type(e).__name__} {str(e)[:200]}")
    if old.get("map"):
        log("지난번 분류를 사용합니다.")
        return old["map"], old.get("source", "cache") + "(이전)"
    log("업종 분류를 하나도 얻지 못했습니다.")
    return {}, "none"


def sector_returns(codes, sectors, weights, close_wide, periods):
    """close_wide: 날짜×종목 종가. 반환: (종목별 dict, 업종 요약 dict)"""
    per_code = {c: {} for c in codes}
    summary = {}
    sec = pd.Series(sectors, index=codes)
    w = pd.Series(weights, index=codes).astype(float)
    w = w.where(w > 0)
    for n in periods:
        if len(close_wide) <= n:
            continue
        last, prev = close_wide.iloc[-1], close_wide.iloc[-1 - n]
        r = ((last / prev - 1) * 100).reindex(codes)
        ok = r.notna() & w.notna() & sec.notna()
        df = pd.DataFrame({"sec": sec, "r": r, "w": w})[ok]
        df["wr"] = df["w"] * df["r"]
        g = df.groupby("sec").agg(W=("w", "sum"), WR=("wr", "sum"), N=("r", "size"))
        for c in codes:
            s = sectors.get(c) if isinstance(sectors, dict) else None
            if c not in df.index or s not in g.index:
                continue
            W, WR, N = g.loc[s, "W"], g.loc[s, "WR"], g.loc[s, "N"]
            wi, ri = df.at[c, "w"], df.at[c, "r"]
            if N - 1 < MIN_GROUP - 1 or W - wi <= 0:
                continue
            sr = (WR - wi * ri) / (W - wi)
            per_code[c][f"sec_ret{n}"] = sr
            per_code[c][f"rel{n}"] = ri - sr
        for s, row in g.iterrows():
            summary.setdefault(s, {"n": int(row["N"])})[f"ret{n}"] = float(row["WR"] / row["W"]) if row["W"] else None
    return per_code, summary
