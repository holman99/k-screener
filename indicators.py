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
]
FIELD_MAP = {f["key"]: f for f in FIELDS}
