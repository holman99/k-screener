"""
웹사이트 생성 스크립트 (GitHub Actions가 하루 3번 자동 실행: 08:00 / 16:00 / 20:00)

1) KOSPI·KOSDAQ 종목 목록과 일봉을 받습니다. (cache/ 에 쌓아 두고 새 날짜만 추가로 받음)
2) 직전 거래일 기준 지표 + 업종 대비 등락률을 계산합니다.
3) 종목별 뉴스(국내 + 대형주 해외)를 모아 48시간·7일 기사 수를 셉니다.
4) site/ 폴더에 웹페이지와 데이터(JSON, 백테스트용 과거 주가 파일)를 만듭니다.

직접 시험할 때:  python build.py --demo   (인터넷 없이 가짜 데이터로 site/ 생성)
"""
import argparse
import gzip
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
CACHE = os.path.join(BASE, "cache")            # GitHub Actions 캐시로 실행 사이에 유지됩니다
PRICES = os.path.join(CACHE, "prices")
KST = timezone(timedelta(hours=9))

HISTORY_DAYS = 620       # 보관할 기간(달력 일수) ≈ 420거래일
CHART_ROWS = 400         # 종목별 차트 파일에 담을 거래일 수
CLOSE_ROWS = 121         # '최근 N일' 조건용 종가 (N 최대 120)
WORKERS = 8
MARKET_CLOSE = (15, 40)
HIST_MIN_AMOUNT = 1.0    # 백테스트 대상: 최근 60일 거래대금 중앙값 1억원 이상
FLOW_ROWS = 20           # 화면용 수급 기간(최근 N일, 최대 20)
FLOW_BT_DAYS = 60        # 수급이 이만큼 쌓이면 백테스트에 포함


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


# ================================================================ 주가 수집
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


def read_cached(code):
    p = os.path.join(PRICES, f"{code}.csv")
    if not os.path.exists(p):
        return None
    try:
        return pd.read_csv(p, parse_dates=["Date"], index_col="Date")
    except Exception:
        return None


def fetch_prices(code):
    """캐시에 있으면 최근 10일치만 새로 받아 이어 붙입니다."""
    import FinanceDataReader as fdr
    old = read_cached(code)
    if old is not None and len(old):
        start = (old.index[-1] - timedelta(days=10)).strftime("%Y-%m-%d")
    else:
        start = (now_kst() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    err = None
    for attempt in range(3):
        try:
            new = fdr.DataReader(code, start)
            break
        except Exception as e:
            err = e
            time.sleep(1 + attempt * 3)
    else:
        if old is not None:
            return old, False
        raise err
    if new is None or new.empty:
        return old, True
    new = new[["Open", "High", "Low", "Close", "Volume"]].copy()
    new.index = pd.to_datetime(new.index)
    new.index.name = "Date"
    df = new if old is None else pd.concat([old, new])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index >= pd.Timestamp(now_kst().date()) - pd.Timedelta(days=HISTORY_DAYS)]
    df.to_csv(os.path.join(PRICES, f"{code}.csv"))
    return df, True


def collect_real():
    os.makedirs(PRICES, exist_ok=True)
    listing = fetch_listing()
    log(f"종목 {len(listing)}개, 일봉 받는 중…")
    prices, errors = {}, []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_prices, c): c for c in listing["code"]}
        for n, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                df, fresh = fut.result()
                if df is not None:
                    prices[code] = df
                if not fresh:
                    errors.append(code)
            except Exception as e:
                errors.append(code)
                log(code, "실패:", e)
            if n % 500 == 0:
                log(f"  {n}/{len(futs)}")
    if len(prices) < len(listing) * 0.5:
        raise RuntimeError(f"절반 이상 받지 못했습니다 (성공 {len(prices)} / {len(listing)}).")
    return listing, prices, errors


# ================================================================ 데모 데이터
DEMO_SECTORS = ["반도체와반도체장비", "제약", "자동차", "화학", "소프트웨어", "건설", "은행", "조선"]


def collect_demo():
    rng = np.random.default_rng(7)
    end = pd.Timestamp(now_kst().date())
    dates = pd.bdate_range(end=end, periods=420)
    names = ["가람전자", "누리바이오", "다온화학", "라온소프트", "마루건설", "바다해운", "사랑식품",
             "아라제약", "자연에너지", "차세대반도체", "하늘항공", "한빛증권", "새롬엔터", "푸른게임",
             "온누리통신", "미래로봇", "별빛화장품", "솔빛중공업", "은하조선", "다솜리츠"]
    recs, prices = [], {}
    sec_factor = {s: rng.normal(0, 0.012, len(dates)) for s in DEMO_SECTORS}
    for i in range(80):
        code = f"9{i:04d}0"
        sec = DEMO_SECTORS[i % len(DEMO_SECTORS)]
        p0 = float(rng.choice([3000, 12000, 45000, 150000]))
        drift = rng.normal(0, 0.0015, len(dates)).cumsum() / 40 + rng.normal(0.0002, 0.0008)
        ret = drift + sec_factor[sec] + rng.normal(0, rng.uniform(0.01, 0.03), len(dates))
        close = p0 * np.exp(np.cumsum(ret))
        op = close * (1 + rng.normal(0, 0.006, len(dates)))
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.01, len(dates))))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.01, len(dates))))
        vol = rng.lognormal(12, 0.5, len(dates)) * (1 + 4 * (rng.random(len(dates)) > 0.97))
        df = pd.DataFrame({"Open": op.round(), "High": hi.round(), "Low": lo.round(),
                           "Close": close.round(), "Volume": vol.round()}, index=dates)
        start = 0 if i % 13 else 300            # 일부는 최근 상장 종목처럼
        prices[code] = df.iloc[start:]
        recs.append({"code": code, "name": f"[데모]{names[i % 20]}{'' if i < 20 else i // 20 + 1}",
                     "market": "KOSPI" if i % 2 == 0 else "KOSDAQ", "sector": sec,
                     "marcap": round(float(close[-1]) * rng.uniform(1e6, 5e7) / 1e8)})
    return pd.DataFrame(recs), prices, []


def demo_flows(codes, used, base_date):
    rng = np.random.default_rng(5)
    out = {}
    for c in codes:
        df = used[c].tail(15)
        vol = df["Volume"].values
        frg = (rng.normal(0, 0.08, len(df)) * vol).round()
        inst = (rng.normal(0, 0.06, len(df)) * vol).round()
        out[c] = pd.DataFrame({"ind": -(frg + inst), "frg": frg, "inst": inst, "est": 0},
                              index=[d.strftime("%Y-%m-%d") for d in df.index])
    return out, {"status": "demo"}


def demo_news(codes):
    rng = np.random.default_rng(3)
    now = now_kst()
    out = {}
    heads = ["신규 수주 공시", "3분기 실적 발표", "목표주가 상향", "외국인 순매수 지속", "신제품 출시",
             "업황 회복 기대", "대표이사 인터뷰", "자사주 매입 결정", "증권가 '저평가' 분석", "공급계약 체결"]
    for i, c in enumerate(codes):
        n = int(rng.poisson(3 + (12 if i % 9 == 0 else 0)))
        items = []
        for k in range(n):
            hours = float(rng.uniform(0, 168)) if i % 9 else float(rng.exponential(30))
            items.append({"t": f"{heads[k % len(heads)]} … 데모 기사 {k + 1}", "s": ["연합뉴스", "한국경제", "매일경제", "이데일리"][k % 4],
                          "d": now - timedelta(hours=min(hours, 167)), "u": "https://finance.naver.com/", "g": 0})
        if i % 5 == 0:
            for k in range(int(rng.poisson(2))):
                items.append({"t": f"Demo global headline {k + 1}", "s": "Reuters", "u": "https://news.google.com/",
                              "d": now - timedelta(hours=float(rng.uniform(0, 160))), "g": 1})
        out[c] = {"items": sorted(items, key=lambda x: x["d"], reverse=True), "capped": False,
                  "fetched_at": now.strftime("%Y-%m-%dT%H:%M")}
    status = {"domestic": "demo", "global": "demo", "eng_names": {c: "Demo" for c in codes[::5]}}
    return out, status


# ================================================================ 출력 도우미
def num(v, nd=4):
    if v is None:
        return None
    if isinstance(v, str):
        return v
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


def write_hist(path_bin, path_meta, used, snap, version, flows=None):
    """백테스트용 과거 주가를 한 파일로 압축합니다 (브라우저가 받아서 직접 계산)."""
    keep = []
    for c in snap["code"]:
        df = used[c]
        amt = (df["Close"] * df["Volume"] / 1e8).tail(60).median()
        if amt >= HIST_MIN_AMOUNT:
            keep.append(c)
    dates = sorted(set().union(*[used[c].index for c in keep]))
    idx = pd.DatetimeIndex(dates)
    D = len(idx)
    blocks = []
    for c in keep:
        df = used[c].reindex(idx)
        valid = df["Close"].notna().values
        cf = df["Close"].ffill().fillna(0).round().astype(np.int64).values
        cd = np.diff(cf, prepend=0)
        def rel(col):
            return np.where(valid, (df[col].fillna(0).round().astype(np.int64).values - cf), 0)
        v = np.where(valid, np.clip(df["Volume"].fillna(0).round(), 0, 2**31 - 1), -1).astype(np.int64)
        blocks += [cd, rel("Open"), rel("High"), rel("Low"), v]
        if flows is not None:
            fl = flows.get(c)
            for k in ("ind", "frg", "inst"):
                if fl is None:
                    blocks.append(np.full(D, -2**31, dtype=np.int64))
                else:
                    q = fl[k].copy()
                    q.index = pd.to_datetime(q.index)
                    q = q.reindex(idx)
                    blocks.append(np.where(q.notna(), np.clip(q.fillna(0).round(), -2**31 + 1, 2**31 - 1), -2**31).astype(np.int64))
    arr = np.concatenate(blocks).astype("<i4")
    shuffled = arr.view(np.uint8).reshape(-1, 4).T.copy().tobytes()   # 바이트 순서 재배열 → 압축률 향상
    with open(path_bin, "wb") as f:
        f.write(gzip.compress(shuffled, 9))
    rows = snap.set_index("code")
    write_json(path_meta, {
        "version": version, "D": D, "dates": [d.strftime("%Y-%m-%d") for d in idx], "codes": keep,
        "market": [rows.at[c, "market"] for c in keep],
        "sector": [num(rows.at[c, "sector"]) if "sector" in rows else None for c in keep],
        "marcap": [num(rows.at[c, "marcap"]) for c in keep],
        "spac": [1 if "스팩" in str(rows.at[c, "name"]) else 0 for c in keep],
        "flow": flows is not None,
    })
    return len(keep), D, os.path.getsize(path_bin)


# ================================================================ 빌드
def build(listing, prices, errors, mode):
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    shutil.copytree(WEB, SITE)
    data_dir = os.path.join(SITE, "data")

    # ---- 지표
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
    stale = snap["date"] < base_date
    snap = snap[~stale].reset_index(drop=True)
    codes = list(snap["code"])
    log(f"기준일 {base_date}, 스크리닝 대상 {len(snap)}종목 (제외 {int(stale.sum())})")

    # ---- 업종
    import sector as sec
    if mode == "demo":
        smap, ssrc = dict(zip(listing["code"], listing["sector"])), "demo"
    else:
        smap, ssrc = sec.load_map(CACHE)
    snap["sector"] = snap["code"].map(smap)
    close_wide = pd.DataFrame({c: used[c]["Close"] for c in codes}).sort_index()
    weights = snap["marcap"].where(snap["marcap"] > 0, snap["amount"]).fillna(1.0)
    per_code, sec_summary = sec.sector_returns(codes, dict(zip(codes, snap["sector"])), list(weights),
                                               close_wide, ind.SECTOR_PERIODS)
    for k in ["sec_ret2", "sec_ret5", "sec_ret20", "rel2", "rel5", "rel20"]:
        snap[k] = snap["code"].map(lambda c: per_code.get(c, {}).get(k, np.nan))
    members = snap.dropna(subset=["sector"]).groupby("sector")["code"].apply(list).to_dict()
    write_json(os.path.join(data_dir, "sectors.json"), {
        s: {**{k: num(v) for k, v in d.items()}, "members": members.get(s, [])} for s, d in sec_summary.items()})

    # ---- 뉴스
    if mode == "demo":
        news, nstatus = demo_news(codes)
    else:
        import news as newsmod
        try:
            news, nstatus = newsmod.collect(snap, CACHE)
        except Exception as e:
            log("뉴스 수집 전체 실패:", e)
            news, nstatus = {}, {"domestic": "error", "global": "error", "message": str(e)[:200]}
    import news as newsmod
    eng = nstatus.get("eng_names", {}) or {}
    cnt = {}
    for c, d in news.items():
        items = d["items"]
        n48, n7 = newsmod.count(items, 0)
        g48, g7 = newsmod.count(items, 1)
        dom = [i for i in items if not i.get("g")]
        capped = bool(d.get("capped")) and dom and min(i["d"] for i in dom) > now_kst() - timedelta(days=7)
        cnt[c] = {"news48": n48, "news7d": n7, "news_cap": 1 if capped else 0,
                  "news_ratio": (n48 / 2) / (n7 / 7) if n7 >= 3 else np.nan,
                  "gnews48": g48 if c in eng else np.nan, "gnews7d": g7 if c in eng else np.nan}
        doms = [i for i in items if not i.get("g")][:80]
        globs = [i for i in items if i.get("g")][:30]
        write_json(os.path.join(data_dir, "news", f"{c}.json"), {
            "fetched_at": d.get("fetched_at"), "capped": bool(capped), "eng": eng.get(c, ""),
            "items": [{"t": i["t"], "s": i.get("s", ""), "d": i["d"].strftime("%Y-%m-%d %H:%M"),
                       "u": i.get("u", ""), "g": i.get("g", 0)} for i in doms + globs]})
    for k in ["news48", "news7d", "news_cap", "news_ratio", "gnews48", "gnews7d"]:
        snap[k] = snap["code"].map(lambda c: cnt.get(c, {}).get(k, np.nan))

    # ---- 수급 (개인·외국인·기관)
    if mode == "demo":
        flows, fstatus = demo_flows(codes, used, base_date)
    else:
        import flows as flowmod
        try:
            flows, fstatus = flowmod.collect(snap, CACHE, base_date)
        except Exception as e:
            log("수급 수집 전체 실패:", e)
            flows, fstatus = {}, {"status": "error", "message": str(e)[:200]}
    fdates = sorted(set().union(*[set(df.index) for df in flows.values()])) if flows else []
    flow_days = len(fdates)
    fd = fdates[-FLOW_ROWS:]
    if flows:
        fj = {"dates": fd, "codes": [], "ind": [], "frg": [], "inst": [], "amt": []}
        for c in codes:
            df = flows.get(c)
            if df is None:
                continue
            px = used[c].copy()
            px.index = px.index.strftime("%Y-%m-%d")
            px = px.reindex(fd)
            sub = df.reindex(fd)
            fj["codes"].append(c)
            for k in ("ind", "frg", "inst"):
                fj[k].append([num(v, 6) for v in (sub[k] * px["Close"] / 1e8)])
            fj["amt"].append([num(v, 6) for v in (px["Close"] * px["Volume"] / 1e8)])
        write_json(os.path.join(data_dir, "flows.json"), fj)
    log(f"수급: 종목 {len(flows)}개, 누적 {flow_days}거래일 (마지막 {fdates[-1] if fdates else '-'})")

    # ---- 스냅샷
    cols = ["code", "name", "market"] + [c for c in snap.columns if c not in ("code", "name", "market", "date")]
    write_json(os.path.join(data_dir, "snapshot.json"), {
        "cols": cols,
        "rows": [[r[0], r[1], r[2]] + [num(v) for v in r[3:]] for r in snap[cols].itertuples(index=False)],
    })

    closes = close_wide.tail(CLOSE_ROWS)
    write_json(os.path.join(data_dir, "closes.json"), {
        "dates": [d.strftime("%Y-%m-%d") for d in closes.index],
        "codes": codes, "data": [[num(v) for v in closes[c]] for c in codes],
    })
    for c in codes:
        d = used[c].tail(CHART_ROWS)
        write_json(os.path.join(data_dir, "c", f"{c}.json"), {
            "d": [t.strftime("%Y-%m-%d") for t in d.index],
            "o": [num(v, 2) for v in d["Open"]], "h": [num(v, 2) for v in d["High"]],
            "l": [num(v, 2) for v in d["Low"]], "c": [num(v, 2) for v in d["Close"]],
            "v": [num(v, 0) for v in d["Volume"]],
        })

    # ---- 백테스트용 과거 데이터
    hv = base_date.replace("-", "") + now_kst().strftime("%H")
    hn, hd, hsize = write_hist(os.path.join(data_dir, "hist.bin.gz"), os.path.join(data_dir, "hist.json"),
                               used, snap, hv, flows if flows else None)
    log(f"백테스트 데이터: {hn}종목 × {hd}일, {hsize / 1e6:.1f}MB")

    fields = [dict(f) for f in ind.FIELDS]
    for f in fields:
        if f["key"].startswith("flow_"):
            f["bt"] = flow_days >= FLOW_BT_DAYS
            f["pmax"] = max(1, min(FLOW_ROWS, flow_days))
            f["pdef"] = min(f.get("pdef", 5), f["pmax"])
    write_json(os.path.join(data_dir, "fields.json"), fields)
    write_json(os.path.join(data_dir, "meta.json"), {
        "mode": mode, "base_date": base_date,
        "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "count": len(snap), "failed": len(errors), "excluded_stale": int(stale.sum()),
        "ichi_shift": ind.ICHI_SHIFT, "sector_source": ssrc,
        "news": {k: v for k, v in nstatus.items() if k != "eng_names"},
        "hist": {"version": hv, "stocks": hn, "days": hd, "bytes": hsize},
        "flows": {**{k: v for k, v in fstatus.items()}, "days": flow_days, "last": fdates[-1] if fdates else None,
                  "bt_days": FLOW_BT_DAYS},
    })
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
