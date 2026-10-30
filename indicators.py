"""
차트 지표 계산 모듈
- 한 종목의 일봉(시가/고가/저가/종가/거래량)을 받아 각종 지표를 계산합니다.
- snapshot_row(): 직전 거래일 기준으로 스크리닝에 쓰일 값 한 줄을 만듭니다.
- FIELDS: 화면에서 고를 수 있는 조건 목록(이름, 단위, 설명)입니다.
"""
import numpy as np
import pandas as pd

MA_WINDOWS = (5, 20, 60, 120)
ICHI_SHIFT = 26          # 일목균형표 선행/후행 이동 칸 수 (HTS에 따라 25를 쓰기도 함)
CROSS_LOOKBACK = 120     # 골든/데드크로스 경과일을 찾을 최대 기간(거래일)
SECTOR_PERIODS = (2, 5, 20)   # 업종 비교 기간(거래일). 2거래일 ≈ 48시간, 5거래일 ≈ 1주일


# ---------------------------------------------------------------- 기본 함수
def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0, np.nan)
    r = 100 - 100 / (1 + rs)
    r = r.where(ad != 0, 100.0)            # 하락이 한 번도 없으면 100
    r = r.where(~((ad == 0) & (au == 0)), 50.0)
    r[au.isna()] = np.nan
    return r


def _days_since_cross(diff, up=True, lookback=CROSS_LOOKBACK):
    """diff = A - B. A가 B를 위로(up) 또는 아래로 뚫은 뒤 며칠 지났는지.
    오늘 발생 = 0. 현재 그 상태가 아니면 NaN."""
    s = np.sign(diff.values[-(lookback + 1):])
    if len(s) < 2 or np.isnan(s[-1]):
        return np.nan
    if (up and s[-1] <= 0) or ((not up) and s[-1] >= 0):
        return np.nan
    for k in range(len(s) - 1, 0, -1):
        prev = s[k - 1]
        if np.isnan(prev):
            return np.nan
        if (up and prev <= 0) or ((not up) and prev >= 0):
            return float(len(s) - 1 - k)
    return np.nan


# ---------------------------------------------------------------- 캔들 패턴
PAT_LOOKBACK = 20        # '최근 N일 안에' 최대 N
PAT_NONE = 99            # 최근 20일 안에 없음
PATTERNS = ["pat_hammer", "pat_invhammer", "pat_doji", "pat_bigup", "pat_bigdn", "pat_engulf_up", "pat_engulf_dn",
            "pat_harami_up", "pat_harami_dn", "pat_mstar", "pat_estar", "pat_gapup", "pat_gapdn"]


def candle_patterns(df):
    """날짜별 패턴 발생 여부(True/False). 브라우저 백테스트(bt-engine.js)와 같은 정의입니다."""
    o, h, l, c = (df[k].astype(float) for k in ("Open", "High", "Low", "Close"))
    R = h - l
    body = (c - o).abs()
    up = h - np.maximum(o, c)
    lo = np.minimum(o, c) - l
    valid = R > 0
    o1, h1, l1, c1 = o.shift(1), h.shift(1), l.shift(1), c.shift(1)
    body1, R1 = (c1 - o1).abs(), h1 - l1
    o2, c2 = o.shift(2), c.shift(2)
    body2, R2 = (c2 - o2).abs(), h.shift(2) - l.shift(2)
    mid2 = (o2 + c2) / 2
    P = pd.DataFrame(index=df.index)
    P["pat_hammer"] = valid & (lo >= 2 * body) & (lo >= 0.6 * R) & (up <= 0.15 * R)
    P["pat_invhammer"] = valid & (up >= 2 * body) & (up >= 0.6 * R) & (lo <= 0.15 * R)
    P["pat_doji"] = valid & (body <= 0.1 * R)
    P["pat_bigup"] = valid & (c > o) & (body >= 0.7 * R) & ((c - o) / o >= 0.05)
    P["pat_bigdn"] = valid & (c < o) & (body >= 0.7 * R) & ((o - c) / o >= 0.05)
    P["pat_engulf_up"] = (c1 < o1) & (c > o) & (o <= c1) & (c >= o1) & (body > body1)
    P["pat_engulf_dn"] = (c1 > o1) & (c < o) & (o >= c1) & (c <= o1) & (body > body1)
    P["pat_harami_up"] = (c1 < o1) & (body1 >= 0.6 * R1) & (c > o) & (np.maximum(o, c) <= o1) & (np.minimum(o, c) >= c1) & (body <= 0.5 * body1)
    P["pat_harami_dn"] = (c1 > o1) & (body1 >= 0.6 * R1) & (c < o) & (np.maximum(o, c) <= c1) & (np.minimum(o, c) >= o1) & (body <= 0.5 * body1)
    P["pat_mstar"] = (c2 < o2) & (body2 >= 0.6 * R2) & (body1 <= 0.3 * body2) & ((o1 + c1) / 2 < c2) & (c > o) & (c >= mid2)
    P["pat_estar"] = (c2 > o2) & (body2 >= 0.6 * R2) & (body1 <= 0.3 * body2) & ((o1 + c1) / 2 > c2) & (c < o) & (c <= mid2)
    P["pat_gapup"] = l > h1
    P["pat_gapdn"] = h < l1
    return P.fillna(False).astype(bool)


def _streak(mask):
    n = 0
    for v in mask[::-1]:
        if not v:
            break
        n += 1
    return float(n)


# ---------------------------------------------------------------- 전체 지표
def full_indicators(df):
    """df: Date 인덱스, Open/High/Low/Close/Volume 열"""
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)
    o = pd.DataFrame(index=df.index)

    # 이동평균 + 1차/2차 미분
    for n in MA_WINDOWS:
        ma = c.rolling(n).mean()
        d1 = ma.pct_change(fill_method=None) * 100      # 하루 동안 이평선 변화율(%)
        o[f"ma{n}"] = ma
        o[f"ma{n}_d1"] = d1
        o[f"ma{n}_d2"] = d1.diff()                       # 기울기의 변화(%p)

    # MACD(12,26,9) — 가격 수준이 달라도 비교되도록 종가 대비 %로 환산
    m = _ema(c, 12) - _ema(c, 26)
    sig = _ema(m, 9)
    hist = m - sig
    o["macd"], o["macd_signal"], o["macd_hist"] = m, sig, hist
    o["macd_pct"] = m / c * 100
    o["macd_d1"] = m.diff() / c * 100
    o["macd_d2"] = m.diff().diff() / c * 100
    o["macd_hist_pct"] = hist / c * 100
    o["macd_hist_d1"] = hist.diff() / c * 100

    # RSI(14)
    o["rsi"] = _rsi(c)
    o["rsi_d1"] = o["rsi"].diff()

    # 볼린저밴드(20, 2)
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    o["bb_mid"], o["bb_up"], o["bb_dn"] = mid, mid + 2 * sd, mid - 2 * sd
    width = (o["bb_up"] - o["bb_dn"]).replace(0, np.nan)
    o["bb_pctb"] = (c - o["bb_dn"]) / width
    o["bb_bw"] = width / mid * 100
    o["bb_bw_rel"] = o["bb_bw"] / o["bb_bw"].rolling(120, min_periods=60).min()

    # 일목균형표(9, 26, 52)
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a = (tenkan + kijun) / 2
    span_b = (h.rolling(52).max() + l.rolling(52).min()) / 2
    o["tenkan"], o["kijun"] = tenkan, kijun
    o["span_a_raw"], o["span_b_raw"] = span_a, span_b          # 26칸 앞에 그려지는 값
    o["cloud_a"] = span_a.shift(ICHI_SHIFT)                      # 오늘 위치의 구름
    o["cloud_b"] = span_b.shift(ICHI_SHIFT)

    # 거래량
    o["vol_ma20"] = v.rolling(20).mean()
    o["vol_ratio"] = v / v.shift(1).rolling(20).mean()
    o["amount"] = c * v / 1e8                                    # 거래대금(억원, 근사)

    # 52주
    o["hi52"] = h.rolling(250, min_periods=60).max()
    o["lo52"] = l.rolling(250, min_periods=60).min()
    return o


def snapshot_row(df, ind=None):
    """직전 거래일 기준 스크리닝 값 한 줄(dict)"""
    if ind is None:
        ind = full_indicators(df)
    c_s = df["Close"].astype(float)
    last = ind.iloc[-1]
    c = float(c_s.iloc[-1])
    r = {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": c,
        "chg1": (c / c_s.iloc[-2] - 1) * 100 if len(c_s) > 1 and c_s.iloc[-2] else np.nan,
        "volume": float(df["Volume"].iloc[-1]),
        "amount": last["amount"],
    }
    for n in MA_WINDOWS:
        ma = last[f"ma{n}"]
        r[f"ma{n}_gap"] = (c / ma - 1) * 100 if ma and not np.isnan(ma) else np.nan
        r[f"ma{n}_d1"] = last[f"ma{n}_d1"]
        r[f"ma{n}_d2"] = last[f"ma{n}_d2"]
    mas = [last[f"ma{n}"] for n in MA_WINDOWS]
    if any(np.isnan(x) for x in mas):
        r["ma_align_up"] = r["ma_align_dn"] = np.nan
    else:
        r["ma_align_up"] = float(all(mas[i] > mas[i + 1] for i in range(len(mas) - 1)))
        r["ma_align_dn"] = float(all(mas[i] < mas[i + 1] for i in range(len(mas) - 1)))
    r["gc_5_20"] = _days_since_cross(ind["ma5"] - ind["ma20"], True)
    r["dc_5_20"] = _days_since_cross(ind["ma5"] - ind["ma20"], False)
    r["gc_20_60"] = _days_since_cross(ind["ma20"] - ind["ma60"], True)
    r["dc_20_60"] = _days_since_cross(ind["ma20"] - ind["ma60"], False)

    for k in ("macd_pct", "macd_d1", "macd_d2", "macd_hist_pct", "macd_hist_d1",
              "rsi", "rsi_d1", "bb_pctb", "bb_bw", "bb_bw_rel", "vol_ratio"):
        r[k] = last[k]
    r["macd_above0"] = float(last["macd"] > 0) if not np.isnan(last["macd"]) else np.nan
    r["gc_macd"] = _days_since_cross(ind["macd_hist"], True)
    r["dc_macd"] = _days_since_cross(ind["macd_hist"], False)

    ca, cb = last["cloud_a"], last["cloud_b"]
    if np.isnan(ca) or np.isnan(cb):
        r["cloud_pos"] = r["cloud_gap"] = np.nan
    else:
        top, bot = max(ca, cb), min(ca, cb)
        r["cloud_pos"] = 1.0 if c > top else (-1.0 if c < bot else 0.0)
        r["cloud_gap"] = (c / top - 1) * 100
    tk, kj = last["tenkan"], last["kijun"]
    r["tk_gap"] = (tk / kj - 1) * 100 if kj and not np.isnan(kj) else np.nan
    r["gc_tk"] = _days_since_cross(ind["tenkan"] - ind["kijun"], True)
    past = c_s.shift(ICHI_SHIFT).iloc[-1]
    r["chikou"] = (c / past - 1) * 100 if past and not np.isnan(past) else np.nan
    sa, sb = last["span_a_raw"], last["span_b_raw"]
    r["cloud_future_up"] = float(sa > sb) if not (np.isnan(sa) or np.isnan(sb)) else np.nan

    r["hi52_gap"] = (c / last["hi52"] - 1) * 100 if not np.isnan(last["hi52"]) else np.nan
    r["lo52_gap"] = (c / last["lo52"] - 1) * 100 if not np.isnan(last["lo52"]) else np.nan
    for n in SECTOR_PERIODS:
        p = c_s.iloc[-1 - n] if len(c_s) > n else np.nan
        r[f"ret{n}"] = (c / p - 1) * 100 if p and not np.isnan(p) else np.nan
    P = candle_patterns(df.tail(PAT_LOOKBACK + 3)).tail(PAT_LOOKBACK)
    for k in PATTERNS:
        hits = np.flatnonzero(P[k].values)
        r[k] = float(len(P) - 1 - hits[-1]) if len(hits) else float(PAT_NONE)
    o_, c_ = df["Open"].astype(float).values, c_s.values
    r["up_streak"] = _streak(c_ > o_)
    r["dn_streak"] = _streak(c_ < o_)
    return r


# ---------------------------------------------------------------- 조건 목록
def _f(group, key, label, typ="num", unit="", desc="", **extra):
    d = dict(group=group, key=key, label=label, type=typ, unit=unit, desc=desc)
    d.update(extra)
    return d


FIELDS = [
    _f("기본", "close", "종가", unit="원", desc="직전 거래일 종가"),
    _f("기본", "chg1", "전일 대비 등락률", unit="%"),
    _f("기본", "amount", "거래대금", unit="억원", desc="종가 × 거래량으로 계산한 근사치"),
    _f("기본", "marcap", "시가총액", unit="억원"),

    _f("기간 등락률", "ret_n", "최근 N일 등락률", unit="%", param="N",
       desc="N 거래일 전 종가 대비 직전일 종가의 변화율. +면 상승, -면 하락"),
    _f("기간 등락률", "hi_n", "최근 N일 최고 종가 대비", unit="%", param="N",
       desc="직전일 종가 ÷ (그 전 N일간 가장 높은 종가). 0보다 크면 N일 종가 신고가"),
    _f("기간 등락률", "lo_n", "최근 N일 최저 종가 대비", unit="%", param="N",
       desc="직전일 종가 ÷ (그 전 N일간 가장 낮은 종가). 0보다 작으면 N일 종가 신저가"),
]
for _n in MA_WINDOWS:
    FIELDS += [
        _f("이동평균선", f"ma{_n}_gap", f"{_n}일선 이격도", unit="%",
           desc=f"종가가 {_n}일선보다 몇 % 위(+)/아래(-)에 있는지"),
        _f("이동평균선", f"ma{_n}_d1", f"{_n}일선 기울기 (1차 미분)", unit="%",
           desc=f"{_n}일선이 하루 동안 몇 % 움직였는지. +면 상승 중, -면 하락 중"),
        _f("이동평균선", f"ma{_n}_d2", f"{_n}일선 가속도 (2차 미분)", unit="%p",
           desc="기울기의 하루 변화. +면 상승이 빨라지거나 하락이 둔해지는 중"),
    ]
FIELDS += [
    _f("이동평균선", "ma_align_up", "정배열 (5 > 20 > 60 > 120)", "bool"),
    _f("이동평균선", "ma_align_dn", "역배열 (5 < 20 < 60 < 120)", "bool"),
    _f("이동평균선", "gc_5_20", "5·20일선 골든크로스 경과일", unit="일",
       desc="0 = 직전일에 발생. 현재 5일선이 20일선 위일 때만 값이 있습니다"),
    _f("이동평균선", "dc_5_20", "5·20일선 데드크로스 경과일", unit="일"),
    _f("이동평균선", "gc_20_60", "20·60일선 골든크로스 경과일", unit="일"),
    _f("이동평균선", "dc_20_60", "20·60일선 데드크로스 경과일", unit="일"),

    _f("MACD", "macd_pct", "MACD 값 (종가 대비)", unit="%",
       desc="MACD선 ÷ 종가. 종목마다 가격이 달라 %로 맞췄습니다. +면 0선 위"),
    _f("MACD", "macd_d1", "MACD 1차 미분", unit="%",
       desc="MACD선의 하루 변화량(종가 대비). +면 MACD가 오르는 중"),
    _f("MACD", "macd_d2", "MACD 2차 미분", unit="%",
       desc="1차 미분의 변화. +면 MACD 상승이 가속(또는 하락이 둔화)"),
    _f("MACD", "macd_hist_pct", "MACD − 시그널 (히스토그램)", unit="%",
       desc="+면 MACD선이 시그널선 위"),
    _f("MACD", "macd_hist_d1", "히스토그램 하루 변화", unit="%"),
    _f("MACD", "macd_above0", "MACD 0선 위", "bool"),
    _f("MACD", "gc_macd", "MACD 골든크로스 경과일", unit="일",
       desc="MACD선이 시그널선을 위로 돌파한 뒤 지난 거래일 수 (0 = 직전일)"),
    _f("MACD", "dc_macd", "MACD 데드크로스 경과일", unit="일"),

    _f("RSI", "rsi", "RSI (14일)", desc="30 이하 과매도, 70 이상 과매수가 일반적인 기준"),
    _f("RSI", "rsi_d1", "RSI 하루 변화", unit="p"),

    _f("볼린저밴드", "bb_pctb", "%B (밴드 내 위치)",
       desc="0 = 하단선, 0.5 = 중심선, 1 = 상단선. 1보다 크면 상단 돌파"),
    _f("볼린저밴드", "bb_bw", "밴드폭", unit="%", desc="(상단−하단) ÷ 중심선. 작을수록 수렴"),
    _f("볼린저밴드", "bb_bw_rel", "밴드폭 ÷ 최근 120일 최소 밴드폭", unit="배",
       desc="1에 가까울수록 최근 반년 중 가장 좁게 수렴한 상태(스퀴즈)"),

    _f("일목균형표", "cloud_pos", "구름대 대비 주가 위치", "enum",
       options=[{"v": 1, "t": "구름 위"}, {"v": 0, "t": "구름 안"}, {"v": -1, "t": "구름 아래"}]),
    _f("일목균형표", "cloud_gap", "구름 상단 대비", unit="%", desc="종가가 구름 윗선보다 몇 % 위인지"),
    _f("일목균형표", "tk_gap", "전환선 ÷ 기준선", unit="%", desc="+면 전환선이 기준선 위"),
    _f("일목균형표", "gc_tk", "전환선·기준선 골든크로스 경과일", unit="일"),
    _f("일목균형표", "chikou", "후행스팬 (26일 전 종가 대비)", unit="%",
       desc="+면 후행스팬이 과거 주가 위(강세 신호)"),
    _f("일목균형표", "cloud_future_up", "미래 구름이 양운 (선행A > 선행B)", "bool"),

    _f("거래량", "vol_ratio", "거래량 ÷ 직전 20일 평균", unit="배", desc="3이면 평소의 3배"),
    _f("거래량", "volume", "거래량", unit="주"),

    _f("52주", "hi52_gap", "52주 최고가 대비", unit="%", desc="0에 가까울수록 신고가 근처"),
    _f("52주", "lo52_gap", "52주 최저가 대비", unit="%"),

    _f("업종 비교", "rel2", "업종 대비 초과 등락률 (2거래일)", unit="%p",
       desc="종목 2거래일 등락률 − 같은 업종(자신 제외, 시총 가중) 등락률. 2거래일 ≈ 48시간"),
    _f("업종 비교", "rel5", "업종 대비 초과 등락률 (5거래일)", unit="%p",
       desc="5거래일 ≈ 1주일. +면 업종보다 강했다는 뜻"),
    _f("업종 비교", "rel20", "업종 대비 초과 등락률 (20거래일)", unit="%p"),
    _f("업종 비교", "sec_ret5", "소속 업종 등락률 (5거래일)", unit="%",
       desc="업종 자체가 오르는 중인지(업종 모멘텀) 볼 때"),

    _f("뉴스", "news48", "국내 기사 수 (최근 48시간)", unit="건", bt=False,
       desc="네이버 증권 종목 뉴스 기준. 백테스트에는 쓸 수 없습니다(과거 기록 없음)"),
    _f("뉴스", "news7d", "국내 기사 수 (최근 7일)", unit="건", bt=False),
    _f("뉴스", "news_ratio", "기사 증가 배수 (48시간 ÷ 7일 일평균)", unit="배", bt=False,
       desc="(48시간 기사 수 ÷ 2) ÷ (7일 기사 수 ÷ 7). 1보다 크면 평소보다 많이 언급됨"),
    _f("뉴스", "gnews7d", "해외(영문) 기사 수 (최근 7일)", unit="건", bt=False,
       desc="시가총액 상위 종목만 집계합니다"),
]
PAT_DESC = {
    "pat_hammer": ("아랫꼬리 망치형", "아랫꼬리가 몸통의 2배 이상이고 하루 변동폭의 60% 이상, 윗꼬리는 15% 이하"),
    "pat_invhammer": ("윗꼬리 망치형 (역망치·유성형)", "윗꼬리가 몸통의 2배 이상이고 하루 변동폭의 60% 이상, 아랫꼬리는 15% 이하"),
    "pat_doji": ("십자형 (도지)", "몸통이 하루 변동폭(고가−저가)의 10% 이하"),
    "pat_bigup": ("장대양봉", "시가 대비 +5% 이상 오른 양봉, 몸통이 하루 변동폭의 70% 이상"),
    "pat_bigdn": ("장대음봉", "시가 대비 −5% 이상 내린 음봉, 몸통이 하루 변동폭의 70% 이상"),
    "pat_engulf_up": ("상승장악형", "전날 음봉의 몸통을 오늘 양봉 몸통이 완전히 감쌈"),
    "pat_engulf_dn": ("하락장악형", "전날 양봉의 몸통을 오늘 음봉 몸통이 완전히 감쌈"),
    "pat_harami_up": ("상승잉태형", "전날 큰 음봉 몸통 안에 오늘 작은 양봉(몸통 절반 이하)"),
    "pat_harami_dn": ("하락잉태형", "전날 큰 양봉 몸통 안에 오늘 작은 음봉(몸통 절반 이하)"),
    "pat_mstar": ("샛별형 (3일)", "큰 음봉 → 아래쪽 작은 봉 → 큰 음봉 몸통 절반 위까지 오른 양봉"),
    "pat_estar": ("석별형 (3일)", "큰 양봉 → 위쪽 작은 봉 → 큰 양봉 몸통 절반 아래까지 내린 음봉"),
    "pat_gapup": ("갭상승", "오늘 저가가 전날 고가보다 높음 (빈 공간)"),
    "pat_gapdn": ("갭하락", "오늘 고가가 전날 저가보다 낮음 (빈 공간)"),
}
for _k in PATTERNS:
    FIELDS.append(_f("차트 모양", _k, PAT_DESC[_k][0], "pattern", desc=PAT_DESC[_k][1]))
FIELDS += [
    _f("차트 모양", "up_streak", "연속 양봉 일수 (종가 > 시가)", unit="일"),
    _f("차트 모양", "dn_streak", "연속 음봉 일수 (종가 < 시가)", unit="일"),
]
FLOW_INV = {"ind": "개인", "frg": "외국인", "inst": "기관"}
for _k, _n in FLOW_INV.items():
    FIELDS.append(_f("수급 (개인·외국인·기관)", f"flow_{_k}_ratio_n", f"{_n} 순매수 비중 (최근 N일)", unit="%", param="N",
                     pdef=5, pmax=20, bt=False,
                     desc=f"N일 동안 {_n} 순매수 금액 ÷ 같은 기간 거래대금. +면 매수 우위, −면 매도 우위"))
for _k, _n in FLOW_INV.items():
    FIELDS.append(_f("수급 (개인·외국인·기관)", f"flow_{_k}_days_n", f"{_n} 순매수한 날 수 (최근 N일)", unit="일", param="N",
                     pdef=5, pmax=20, bt=False, desc=f"최근 N일 중 {_n}가 순매수(+)였던 날의 수"))
for _f_ in FIELDS:
    _f_.setdefault("bt", True)
FIELD_MAP = {f["key"]: f for f in FIELDS}
