"""
투자자별 매매동향(개인·외국인·기관 순매수) 수집
- 네이버 증권 앱의 종목별 매매동향(최근 약 10거래일)을 받습니다.
  실패하면 네이버 증권 PC 페이지(기관·외국인만, 개인은 '−(기관+외국인)'으로 추정)를 씁니다.
- 받은 값은 cache/flows/{종목코드}.csv 에 날짜별로 계속 쌓습니다.
  → 쌓인 기간이 60거래일을 넘으면 백테스트에서도 수급 조건을 쓸 수 있게 됩니다.
- 단위는 '주(수량)'로 저장하고, 화면용 금액(억원)은 그날 종가를 곱해 계산합니다.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd

from news import Blocked, Client, now_kst

KEEP_DAYS = 400          # 최대 보관 거래일
WORKERS = 4
ENDPOINTS = [
    ("m-trend", "https://m.stock.naver.com/api/stock/{code}/trend"),
    ("m-trend-paged", "https://m.stock.naver.com/api/stock/{code}/trend?pageSize=60&page=1"),
    ("pc-frgn", "https://finance.naver.com/item/frgn.naver?code={code}&page=1"),
]
DATE_KEYS = ("bizdate", "bizDate", "localTradedAt", "tradeDate", "date", "baseDate", "dt", "tradingDate")
INV_PAT = {
    "frg": re.compile(r"(foreign|frgn|foreigner)", re.I),
    "inst": re.compile(r"(organ|inst|institution)", re.I),
    "ind": re.compile(r"(individual|indi|personal|retail|person)", re.I),
}
NET_PAT = re.compile(r"(pure|net|buy)", re.I)
AMT_PAT = re.compile(r"(amount|amt|value|price|ratio|rate|hold)", re.I)


def log(*a):
    print(now_kst().strftime("%H:%M:%S"), "[수급]", *a, flush=True)


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v):
    s = re.sub(r"[^0-9]", "", str(v))[:8]
    if len(s) == 8:
        try:
            return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def rows_from_json(j):
    """응답 안에서 '날짜 + 개인/외국인/기관 순매수(수량)'가 있는 항목을 찾습니다."""
    out, keys_used = {}, {}
    for d in _walk(j):
        dk = next((k for k in DATE_KEYS if k in d), None)
        if not dk:
            continue
        date = parse_date(d[dk])
        if not date:
            continue
        row = {}
        for inv, pat in INV_PAT.items():
            cands = [k for k in d if pat.search(k) and NET_PAT.search(k) and not AMT_PAT.search(k)]
            if not cands:
                continue
            k = sorted(cands, key=lambda x: (0 if re.search("quant|vol|qty", x, re.I) else 1, len(x)))[0]
            v = num(d[k])
            if v is not None:
                row[inv] = v
                keys_used[inv] = k
        if len(row) >= 2:
            out[date] = row
    return out, keys_used


def rows_from_html(text):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "html.parser")
    out = {}
    for tr in soup.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 7 or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", tds[0]):
            continue
        inst, frg = num(tds[5]), num(tds[6])
        if inst is None or frg is None:
            continue
        out[parse_date(tds[0])] = {"inst": inst, "frg": frg, "ind": -(inst + frg), "est": 1}
    return out


def fetch(client, ep, code):
    name, tpl = ep
    url = tpl.format(code=code)
    if name == "pc-frgn":
        r = client.get(url, referer=f"https://finance.naver.com/item/main.naver?code={code}")
        return rows_from_html(r.content.decode("euc-kr", errors="replace")), {}
    r = client.get(url, referer="https://m.stock.naver.com/")
    return rows_from_json(r.json())


def detect(client, probe="005930"):
    for ep in ENDPOINTS:
        try:
            rows, keys = fetch(client, ep, probe)
            log(f"주소 시험 {ep[0]}: {len(rows)}일치, 사용한 항목 {keys or '(표에서 읽음)'}")
            if len(rows) >= 3 and (ep[0] == "pc-frgn" or len(keys) >= 2):
                return ep
        except Blocked:
            raise
        except Exception as e:
            log(f"주소 시험 {ep[0]} 실패: {type(e).__name__} {str(e)[:150]}")
    return None


def _path(cache_dir, code):
    return os.path.join(cache_dir, "flows", f"{code}.csv")


def load(cache_dir, code):
    p = _path(cache_dir, code)
    if not os.path.exists(p):
        return None
    try:
        return pd.read_csv(p, index_col="date")
    except Exception:
        return None


def save(cache_dir, code, rows):
    old = load(cache_dir, code)
    new = pd.DataFrame.from_dict(rows, orient="index")
    new.index.name = "date"
    for k in ("ind", "frg", "inst", "est"):
        if k not in new:
            new[k] = 0 if k == "est" else float("nan")
    new = new[["ind", "frg", "inst", "est"]]
    df = new if old is None else pd.concat([old[~old.index.isin(new.index)], new])
    df = df.sort_index().tail(KEEP_DAYS)
    df.to_csv(_path(cache_dir, code))
    return df


def collect(snapshot, cache_dir, base_date, min_amount=1.0):
    """반환: (종목별 DataFrame dict, 상태 dict)"""
    os.makedirs(os.path.join(cache_dir, "flows"), exist_ok=True)
    state_path = os.path.join(cache_dir, "flows_state.json")
    try:
        state = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        state = {}
    now = now_kst()
    final_time = now.hour >= 18
    need = state.get("date") != base_date or (final_time and not state.get("final"))
    codes = list(snapshot.loc[snapshot["amount"] >= min_amount, "code"])
    status = {"status": "cached", "endpoint": state.get("endpoint"), "fetched": 0}
    if need:
        client = Client()
        try:
            ep = detect(client)
            if ep is None:
                status["status"] = "no-endpoint"
                log("수급 데이터를 줄 주소를 찾지 못했습니다. 이전에 쌓은 데이터를 씁니다.")
            else:
                status.update(status="ok", endpoint=ep[0])
                log(f"'{ep[0]}' 주소로 {len(codes)}종목 수집")
                fails = 0
                with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                    futs = {ex.submit(fetch, client, ep, c): c for c in codes}
                    for n, fut in enumerate(as_completed(futs), 1):
                        c = futs[fut]
                        try:
                            rows, _ = fut.result()
                            rows = {d: v for d, v in rows.items() if d and d <= base_date}
                            if rows:
                                save(cache_dir, c, rows)
                                status["fetched"] += 1
                        except Blocked as e:
                            status["status"] = "blocked"
                            log("요청이 차단되었습니다:", e, "— 이전 데이터를 씁니다.")
                            for f in futs:
                                f.cancel()
                            break
                        except Exception:
                            fails += 1
                        if n % 500 == 0:
                            log(f"  {n}/{len(codes)}")
                status["failed"] = fails
                if status["status"] == "ok" and status["fetched"] > len(codes) * 0.5:
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump({"date": base_date, "final": final_time, "endpoint": ep[0]}, f)
        except Blocked as e:
            status["status"] = "blocked"
            log("요청이 차단되었습니다:", e)
    else:
        log(f"{base_date} 수급은 이미 받아 두었습니다 (다시 받지 않음).")
    data = {}
    for c in snapshot["code"]:
        df = load(cache_dir, c)
        if df is not None and len(df):
            data[c] = df[df.index <= base_date]
    log(f"수급 보유 종목 {len(data)}개 ({status['status']})")
    return data, status
