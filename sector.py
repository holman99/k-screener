"""
업종 분류 + 업종 대비 등락률
- 분류: 네이버 증권 업종(약 80개, 예: 제약 / 반도체와반도체장비)을 1순위로 씁니다.
  실패하면 지난번 분류를 그대로 쓰고, 그것도 없으면 거래소 업종(FinanceDataReader)을 시도합니다.
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


def parse_group_list(html):
    out, seen = [], set()
    for no, name in re.findall(r'sise_group_detail\.naver\?type=upjong&(?:amp;)?no=(\d+)"[^>]*>([^<]+)</a>', html):
        if no not in seen:
            seen.add(no)
            out.append((no, name.strip()))
    return out


def parse_group_members(html):
    return list(dict.fromkeys(re.findall(r"/item/main\.naver\?code=(\d{6})", html)))


def fetch_naver_map():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    groups = parse_group_list(_get(s, LIST_URL))
    if len(groups) < 20:
        raise RuntimeError(f"업종 목록이 너무 적습니다({len(groups)}개)")
    mp = {}
    for no, name in groups:
        try:
            for code in parse_group_members(_get(s, DETAIL_URL.format(no=no))):
                mp.setdefault(code, name)
        except Exception as e:
            log(name, "실패:", e)
        time.sleep(0.3)
    return mp


def fetch_krx_map():
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX-DESC")
    cols = {c.lower(): c for c in df.columns}
    if "sector" not in cols:
        raise RuntimeError("Sector 열 없음")
    code = cols.get("code") or cols.get("symbol")
    return {str(c).zfill(6): str(s) for c, s in zip(df[code], df[cols["sector"]]) if isinstance(s, str) and s}


def load_map(cache_dir, refresh=True):
    path = os.path.join(cache_dir, "sector_map.json")
    old = {}
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        pass
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if old.get("date") == today or not refresh:
        return old.get("map", {}), old.get("source", "cache")
    for src, fn in (("naver", fetch_naver_map), ("krx", fetch_krx_map)):
        try:
            mp = fn()
            if len(mp) > 500:
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"date": today, "source": src, "map": mp}, f, ensure_ascii=False)
                log(f"{src} 분류 {len(mp)}종목")
                return mp, src
            log(f"{src} 분류가 너무 적어 건너뜀 ({len(mp)})")
        except Exception as e:
            log(f"{src} 분류 실패:", e)
    if old.get("map"):
        log("지난번 분류를 사용합니다.")
        return old["map"], old.get("source", "cache") + "(이전)"
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
