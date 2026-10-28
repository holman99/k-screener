"""
웹사이트 생성 스크립트 (GitHub Actions가 매일 자동 실행)

1) KOSPI·KOSDAQ 종목 목록과 일봉을 FinanceDataReader로 받습니다.
2) 직전 거래일 기준으로 지표를 계산합니다.
3) site/ 폴더에 웹페이지 + 데이터(JSON)를 만들어 GitHub Pages에 올립니다.

직접 시험할 때:  python build.py --demo   (인터넷 없이 가짜 데이터로 site/ 생성)
"""
import argparse
import json
import math
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import indicators as ind

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
SITE = os.path.join(BASE, "site")
CACHE = os.path.join(BASE, "cache")          # 종목 목록 백업 (GitHub Actions 캐시로 유지)
KST = timezone(timedelta(hours=9))

HISTORY_DAYS = 620       # 받을 기간(달력 일수)
CHART_ROWS = 400         # 종목별 차트 파일에 담을 거래일 수
CLOSE_ROWS = 121         # '최근 N일' 조건용 종가 (N 최대 120)
WORKERS = 8
MARKET_CLOSE = (15, 40)


def now_kst():
    return datetime.now(KST)


def log(*a):
    print(now_kst().strftime("%H:%M:%S"), *a, flush=True)


def drop_unfinished_today(df):
    if df is None or df.empty:
        return df
    now = now_kst()
    if df.index[-1].date() == now.date() and (now.hour, now.minute) < MARKET_CLOSE:
        return df.iloc[:-1]
    return df


# ---------------------------------------------------------------- 수집
def fetch_listing():
    import FinanceDataReader as fdr

    def norm(df, market=None):
        cols = {c.lower(): c for c in df.columns}
        code = cols.get("code") or cols.get("symbol")
        out = pd.DataFrame({"code": df[code].astype(str).str.zfill(6),
                            "name": df[cols["name"]].astype(str)})
        out["market"] = market if market else (df[cols["market"]].astype(str) if "market" in cols else np.nan)
        mc = cols.get("marcap")
        out["marcap"] = pd.to_numeric(df[mc], errors="coerce") / 1e8 if mc else np.nan
        return out

    df = None
    try:
        d = norm(fdr.StockListing("KRX"))
        if d["market"].notna().any():
            df = d
    except Exception as e:
        log("StockListing('KRX') 실패:", e)
    if df is None:
        parts = []
        for m in ("KOSPI", "KOSDAQ"):
            try:
                parts.append(norm(fdr.StockListing(m), m))
            except Exception as e:
                log(f"StockListing('{m}') 실패:", e)
        if parts:
            df = pd.concat(parts)

    cache_file = os.path.join(CACHE, "listing.csv")
    if df is None or len(df) < 100:
        if os.path.exists(cache_file):
            log("종목 목록을 받지 못해 지난번 목록을 사용합니다.")
            return pd.read_csv(cache_file, dtype={"code": str})
        raise RuntimeError("종목 목록을 받지 못했습니다.")

    df["market"] = df["market"].str.upper().str.replace(" GLOBAL", "", regex=False)
    df = df[df["market"].isin(["KOSPI", "KOSDAQ"])].drop_duplicates("code").reset_index(drop=True)
    os.makedirs(CACHE, exist_ok=True)
    df.to_csv(cache_file, index=False)
    return df


def fetch_prices(code, start):
    import FinanceDataReader as fdr
    err = None
    for attempt in range(3):
        try:
            df = fdr.DataReader(code, start)
            if df is None or df.empty:
                return None
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = pd.to_datetime(df.index)
            return df[~df.index.duplicated(keep="last")].sort_index()
        except Exception as e:
            err = e
            time.sleep(1 + attempt * 3)
    raise err


def collect_real():
    listing = fetch_listing()
    log(f"종목 {len(listing)}개, 일봉 받는 중…")
    start = (now_kst() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    prices, errors = {}, []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_prices, c, start): c for c in listing["code"]}
        for n, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                df = fut.result()
                if df is not None:
                    prices[code] = df
            except Exception as e:
                errors.append(code)
                log(code, "실패:", e)
            if n % 250 == 0:
                log(f"  {n}/{len(futs)}")
    if len(prices) < len(listing) * 0.5:
        raise RuntimeError(f"절반 이상 받지 못했습니다 (성공 {len(prices)} / {len(listing)}).")
    return listing, prices, errors


def collect_demo():
    rng = np.random.default_rng(7)
    end = pd.Timestamp(now_kst().date())
    dates = pd.bdate_range(end=end, periods=420)
    names = ["가람전자", "누리바이오", "다온화학", "라온소프트", "마루건설", "바다해운", "사랑식품",
             "아라제약", "자연에너지", "차세대반도체", "하늘항공", "한빛증권", "새롬엔터", "푸른게임",
             "온누리통신", "미래로봇", "별빛화장품", "솔빛중공업", "은하조선", "다솜리츠"]
    recs, prices = [], {}
    for i in range(80):
        code = f"9{i:04d}0"
        p0 = float(rng.choice([3000, 12000, 45000, 150000]))
        drift = rng.normal(0, 0.0015, len(dates)).cumsum() / 40 + rng.normal(0.0002, 0.0008)
        close = p0 * np.exp(np.cumsum(drift + rng.normal(0, rng.uniform(0.012, 0.035), len(dates))))
        op = close * (1 + rng.normal(0, 0.006, len(dates)))
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.01, len(dates))))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.01, len(dates))))
        vol = rng.lognormal(12, 0.5, len(dates)) * (1 + 4 * (rng.random(len(dates)) > 0.97))
        prices[code] = pd.DataFrame({"Open": op.round(), "High": hi.round(), "Low": lo.round(),
                                     "Close": close.round(), "Volume": vol.round()}, index=dates)
        recs.append({"code": code, "name": f"[데모]{names[i % 20]}{'' if i < 20 else i // 20 + 1}",
                     "market": "KOSPI" if i % 2 == 0 else "KOSDAQ",
                     "marcap": round(float(close[-1]) * rng.uniform(1e6, 5e7) / 1e8)})
    return pd.DataFrame(recs), prices, []


# ---------------------------------------------------------------- 출력
def num(v, nd=4):
    """JSON 크기를 줄이기 위해 숫자를 적당히 반올림"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if math.isnan(f) or math.isinf(f):
        return None
    if abs(f) >= 1000:
        return int(round(f))
    return round(f, nd)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def build(listing, prices, errors, mode):
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    shutil.copytree(WEB, SITE)
    data_dir = os.path.join(SITE, "data")

    rows, used = [], {}
    for rec in listing.itertuples(index=False):
        df = drop_unfinished_today(prices.get(rec.code))
        if df is None or len(df) < 30:
            continue
        try:
            r = ind.snapshot_row(df)
        except Exception as e:
            log(rec.code, "지표 계산 오류:", e)
            continue
        r.update(code=rec.code, name=rec.name, market=rec.market, marcap=rec.marcap)
        rows.append(r)
        used[rec.code] = df
    snap = pd.DataFrame(rows)
    base_date = snap["date"].mode().iloc[0]
    stale = snap["date"] < base_date                     # 기준일에 거래 없는 종목 제외
    snap = snap[~stale].reset_index(drop=True)
    log(f"기준일 {base_date}, 스크리닝 대상 {len(snap)}종목 (제외 {int(stale.sum())})")

    cols = ["code", "name", "market"] + [c for c in snap.columns if c not in ("code", "name", "market", "date")]
    write_json(os.path.join(data_dir, "snapshot.json"), {
        "cols": cols,
        "rows": [[r[0], r[1], r[2]] + [num(v) for v in r[3:]] for r in snap[cols].itertuples(index=False)],
    })

    # 최근 N일 조건용 종가
    codes = list(snap["code"])
    closes = pd.DataFrame({c: used[c]["Close"] for c in codes}).sort_index().tail(CLOSE_ROWS)
    write_json(os.path.join(data_dir, "closes.json"), {
        "dates": [d.strftime("%Y-%m-%d") for d in closes.index],
        "codes": codes,
        "data": [[num(v) for v in closes[c]] for c in codes],
    })

    # 종목별 차트용 일봉
    for c in codes:
        d = used[c].tail(CHART_ROWS)
        write_json(os.path.join(data_dir, "c", f"{c}.json"), {
            "d": [t.strftime("%Y-%m-%d") for t in d.index],
            "o": [num(v, 2) for v in d["Open"]], "h": [num(v, 2) for v in d["High"]],
            "l": [num(v, 2) for v in d["Low"]], "c": [num(v, 2) for v in d["Close"]],
            "v": [num(v, 0) for v in d["Volume"]],
        })

    write_json(os.path.join(data_dir, "fields.json"), ind.FIELDS)
    write_json(os.path.join(data_dir, "meta.json"), {
        "mode": mode, "base_date": base_date,
        "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "count": len(snap), "failed": len(errors), "excluded_stale": int(stale.sum()),
        "ichi_shift": ind.ICHI_SHIFT,
    })
    # GitHub Pages가 data 폴더를 그대로 내보내도록
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    log("site/ 생성 완료")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 생성")
    a = ap.parse_args()
    t0 = time.time()
    listing, prices, errors = collect_demo() if a.demo else collect_real()
    build(listing, prices, errors, "demo" if a.demo else "real")
    log(f"총 {time.time() - t0:.0f}초")
