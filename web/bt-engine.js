/* 백테스트 엔진 — 서버(Python)와 똑같은 정의로 과거 모든 날의 지표를 다시 계산합니다.
   브라우저(Web Worker)와 테스트(Node) 양쪽에서 씁니다. */
(function (G) {
  "use strict";
  const NaN_ = NaN;
  const MA_WINDOWS = [5, 20, 60, 120];
  const ICHI = 26, LOOKBACK = 120, SECTOR_PERIODS = [2, 5, 20];

  const arr = (n, v = NaN_) => new Float64Array(n).fill(v);
  const isn = (x) => x !== x; // NaN 검사 (빠름)

  /* ---------- 기본 계산 (pandas와 같은 규칙) ---------- */
  function sma(a, w) { // rolling(w).mean(): 창 안에 값이 모두 있어야 함
    const n = a.length, r = arr(n);
    let s = 0, cnt = 0;
    for (let i = 0; i < n; i++) {
      const x = a[i]; if (!isn(x)) { s += x; cnt++; }
      if (i >= w) { const y = a[i - w]; if (!isn(y)) { s -= y; cnt--; } }
      if (i >= w - 1 && cnt === w) {
        // 누적 오차 방지: 주기적으로 다시 합산
        if (i % 64 === 0) { s = 0; for (let k = i - w + 1; k <= i; k++) s += a[k]; }
        r[i] = s / w;
      }
    }
    return r;
  }
  function std0(a, w) { // rolling(w).std(ddof=0)
    const n = a.length, r = arr(n);
    for (let i = w - 1; i < n; i++) {
      let s = 0, ok = true;
      for (let k = i - w + 1; k <= i; k++) { if (isn(a[k])) { ok = false; break; } s += a[k]; }
      if (!ok) continue;
      const m = s / w; let q = 0;
      for (let k = i - w + 1; k <= i; k++) q += (a[k] - m) * (a[k] - m);
      r[i] = Math.sqrt(q / w);
    }
    return r;
  }
  function rollExt(a, w, minp, isMax) { // rolling(w, min_periods=minp).max()/min(), NaN 무시
    const n = a.length, r = arr(n), dq = new Int32Array(n);
    let h = 0, t = 0, cnt = 0;
    for (let i = 0; i < n; i++) {
      const x = a[i];
      if (!isn(x)) {
        cnt++;
        while (t > h && (isMax ? a[dq[t - 1]] <= x : a[dq[t - 1]] >= x)) t--;
        dq[t++] = i;
      }
      if (i >= w && !isn(a[i - w])) cnt--;
      while (t > h && dq[h] <= i - w) h++;
      if (cnt >= minp && t > h) r[i] = a[dq[h]];
    }
    return r;
  }
  function ema(a, span) { // ewm(span, adjust=False), 첫 값에서 시작
    const n = a.length, r = arr(n), k = 2 / (span + 1);
    let prev = NaN_;
    for (let i = 0; i < n; i++) {
      const x = a[i];
      if (isn(prev)) prev = x; else if (!isn(x)) prev = k * x + (1 - k) * prev;
      r[i] = prev;
    }
    return r;
  }
  function diff(a) { const n = a.length, r = arr(n); for (let i = 1; i < n; i++) r[i] = a[i] - a[i - 1]; return r; }
  function pct(a) { const n = a.length, r = arr(n); for (let i = 1; i < n; i++) r[i] = (a[i] / a[i - 1] - 1) * 100; return r; }
  function shift(a, k) { const n = a.length, r = arr(n); for (let i = k; i < n; i++) r[i] = a[i - k]; return r; }
  function rsi(c, p = 14) {
    const n = c.length, r = arr(n), al = 1 / p;
    let au = NaN_, ad = NaN_;
    for (let i = 1; i < n; i++) {
      const d = c[i] - c[i - 1], u = d > 0 ? d : 0, dn = d < 0 ? -d : 0;
      if (i === 1) { au = u; ad = dn; } else { au = (1 - al) * au + al * u; ad = (1 - al) * ad + al * dn; }
      if (i < p) continue;
      let v;
      if (ad === 0 && au === 0) v = 50; else if (ad === 0) v = 100; else v = 100 - 100 / (1 + au / ad);
      r[i] = v;
    }
    return r;
  }
  function daysSince(dif, up) { // 골든(up)/데드크로스 이후 경과 거래일. 오늘 발생 = 0
    const n = dif.length, r = arr(n);
    for (let i = 1; i < n; i++) {
      const s = Math.sign(dif[i]), p = Math.sign(dif[i - 1]);
      if (isn(s) || (up ? s <= 0 : s >= 0) || isn(p)) continue;
      let v;
      if (up ? p <= 0 : p >= 0) v = 0; else v = r[i - 1] + 1;
      if (!isn(v) && v <= LOOKBACK - 1) r[i] = v;
    }
    return r;
  }
  const mid2 = (a, b) => a.map((x, i) => (x + b[i]) / 2);

  /* ---------- 한 종목의 모든 날 지표 ---------- */
  function computeFields(c, o, h, l, v) {
    const n = c.length, F = {};
    F.close = c; F.volume = v;
    F.chg1 = pct(c);
    F.amount = c.map((x, i) => x * v[i] / 1e8);
    const ma = {};
    for (const w of MA_WINDOWS) {
      const m = sma(c, w), d1 = pct(m);
      ma[w] = m;
      F[`ma${w}_gap`] = c.map((x, i) => (m[i] && !isn(m[i]) ? (x / m[i] - 1) * 100 : NaN_));
      F[`ma${w}_d1`] = d1;
      F[`ma${w}_d2`] = diff(d1);
    }
    F.ma_align_up = arr(n); F.ma_align_dn = arr(n);
    for (let i = 0; i < n; i++) {
      const a = MA_WINDOWS.map((w) => ma[w][i]);
      if (a.some(isn)) continue;
      F.ma_align_up[i] = a[0] > a[1] && a[1] > a[2] && a[2] > a[3] ? 1 : 0;
      F.ma_align_dn[i] = a[0] < a[1] && a[1] < a[2] && a[2] < a[3] ? 1 : 0;
    }
    F.gc_5_20 = daysSince(ma[5].map((x, i) => x - ma[20][i]), true);
    F.dc_5_20 = daysSince(ma[5].map((x, i) => x - ma[20][i]), false);
    F.gc_20_60 = daysSince(ma[20].map((x, i) => x - ma[60][i]), true);
    F.dc_20_60 = daysSince(ma[20].map((x, i) => x - ma[60][i]), false);

    const e12 = ema(c, 12), e26 = ema(c, 26);
    const m = e12.map((x, i) => x - e26[i]);
    const sig = ema(m, 9);
    const hist = m.map((x, i) => x - sig[i]);
    const dm = diff(m), ddm = diff(dm), dh = diff(hist);
    F.macd_pct = m.map((x, i) => x / c[i] * 100);
    F.macd_d1 = dm.map((x, i) => x / c[i] * 100);
    F.macd_d2 = ddm.map((x, i) => x / c[i] * 100);
    F.macd_hist_pct = hist.map((x, i) => x / c[i] * 100);
    F.macd_hist_d1 = dh.map((x, i) => x / c[i] * 100);
    F.macd_above0 = m.map((x) => (isn(x) ? NaN_ : x > 0 ? 1 : 0));
    F.gc_macd = daysSince(hist, true);
    F.dc_macd = daysSince(hist, false);

    F.rsi = rsi(c);
    F.rsi_d1 = diff(F.rsi);

    const mid = sma(c, 20), sd = std0(c, 20);
    const up = mid.map((x, i) => x + 2 * sd[i]), dn = mid.map((x, i) => x - 2 * sd[i]);
    const width = up.map((x, i) => { const w = x - dn[i]; return w === 0 ? NaN_ : w; });
    F.bb_pctb = c.map((x, i) => (x - dn[i]) / width[i]);
    F.bb_bw = width.map((x, i) => x / mid[i] * 100);
    const bwMin = rollExt(F.bb_bw, 120, 60, false);
    F.bb_bw_rel = F.bb_bw.map((x, i) => x / bwMin[i]);

    const tenkan = mid2(rollExt(h, 9, 9, true), rollExt(l, 9, 9, false));
    const kijun = mid2(rollExt(h, 26, 26, true), rollExt(l, 26, 26, false));
    const spanA = mid2(tenkan, kijun);
    const spanB = mid2(rollExt(h, 52, 52, true), rollExt(l, 52, 52, false));
    const ca = shift(spanA, ICHI), cb = shift(spanB, ICHI);
    F.cloud_pos = arr(n); F.cloud_gap = arr(n);
    for (let i = 0; i < n; i++) {
      if (isn(ca[i]) || isn(cb[i])) continue;
      const top = Math.max(ca[i], cb[i]), bot = Math.min(ca[i], cb[i]);
      F.cloud_pos[i] = c[i] > top ? 1 : c[i] < bot ? -1 : 0;
      F.cloud_gap[i] = (c[i] / top - 1) * 100;
    }
    F.tk_gap = tenkan.map((x, i) => (kijun[i] && !isn(kijun[i]) ? (x / kijun[i] - 1) * 100 : NaN_));
    F.gc_tk = daysSince(tenkan.map((x, i) => x - kijun[i]), true);
    const past = shift(c, ICHI);
    F.chikou = c.map((x, i) => (past[i] && !isn(past[i]) ? (x / past[i] - 1) * 100 : NaN_));
    F.cloud_future_up = spanA.map((x, i) => (isn(x) || isn(spanB[i]) ? NaN_ : x > spanB[i] ? 1 : 0));

    const vprev = sma(shift(v, 1), 20);
    F.vol_ratio = v.map((x, i) => x / vprev[i]);
    const hi52 = rollExt(h, 250, 60, true), lo52 = rollExt(l, 250, 60, false);
    F.hi52_gap = c.map((x, i) => (x / hi52[i] - 1) * 100);
    F.lo52_gap = c.map((x, i) => (x / lo52[i] - 1) * 100);
    for (const p of SECTOR_PERIODS) F[`ret${p}`] = c.map((x, i) => (i >= p && c[i - p] ? (x / c[i - p] - 1) * 100 : NaN_));
    return F;
  }

  function paramField(key, c, n) {
    const L = c.length, r = arr(L);
    for (let i = n; i < L; i++) {
      if (key === "ret_n") { r[i] = c[i - n] ? (c[i] / c[i - n] - 1) * 100 : NaN_; continue; }
      let ext = key === "hi_n" ? -Infinity : Infinity;
      for (let k = i - n; k < i; k++) ext = key === "hi_n" ? Math.max(ext, c[k]) : Math.min(ext, c[k]);
      r[i] = (c[i] / ext - 1) * 100;
    }
    return r;
  }

  /* ---------- 과거 데이터 파일 풀기 ---------- */
  function unshuffle(u8) {
    const n = u8.length / 4, out = new Uint8Array(u8.length);
    for (let k = 0; k < 4; k++) { const off = k * n; for (let i = 0; i < n; i++) out[4 * i + k] = u8[off + i]; }
    return new Int32Array(out.buffer);
  }
  function stockSeries(H, s) { // 정렬된 날짜 기준 (거래 없는 날 NaN)
    const D = H.D, I = H.I, b = s * 5 * D;
    const C = arr(D), O = arr(D), Hh = arr(D), L = arr(D), V = arr(D);
    let c = 0;
    for (let t = 0; t < D; t++) {
      c += I[b + t];
      const vol = I[b + 4 * D + t];
      if (vol === -1) continue;
      C[t] = c; O[t] = c + I[b + D + t]; Hh[t] = c + I[b + 2 * D + t]; L[t] = c + I[b + 3 * D + t]; V[t] = vol;
    }
    return { C, O, H: Hh, L, V };
  }
  function compact(S) {
    const idx = [];
    for (let t = 0; t < S.C.length; t++) if (!isn(S.C[t])) idx.push(t);
    const pick = (a) => Float64Array.from(idx, (t) => a[t]);
    return { idx, c: pick(S.C), o: pick(S.O), h: pick(S.H), l: pick(S.L), v: pick(S.V) };
  }

  /* ---------- 백테스트 ---------- */
  const MAXK = 10; // 매수일 이후 최대 보유 거래일
  function prepare(H) { // 한 번만: 기준 수익률(전체 종목 평균), 종목별 시계열
    const D = H.D, N = H.meta.codes.length;
    H.series = []; H.bench = [];
    const sum = [], cnt = [];
    for (let k = 0; k <= MAXK; k++) { sum.push(new Float64Array(D)); cnt.push(new Float64Array(D)); }
    for (let s = 0; s < N; s++) {
      const S = stockSeries(H, s); H.series.push(S);
      for (let t = 0; t < D - 1; t++) {
        const e = S.O[t + 1]; if (!(e > 0)) continue;
        for (let k = 0; k <= MAXK; k++) {
          const d = t + 1 + k; if (d >= D) break;
          const x = S.C[d]; if (isn(x)) continue;
          sum[k][t] += x / e - 1; cnt[k][t]++;
        }
      }
    }
    // bench[k][t] = 신호일 t 다음날 시가에 전체 종목을 사서, 그로부터 k거래일 뒤 종가에 판 평균 수익률
    for (let k = 0; k <= MAXK; k++) H.bench.push(sum[k].map((x, t) => (cnt[k][t] ? x / cnt[k][t] : NaN_)));
  }

  function sectorRel(H) { // 날짜별 업종(자신 제외, 시총 가중) 등락률과 초과 등락률
    if (H.rel) return H.rel;
    const D = H.D, N = H.meta.codes.length, secs = H.meta.sector, w = H.meta.marcap.map((x) => (x > 0 ? x : 1));
    const out = {};
    for (const p of SECTOR_PERIODS) {
      const R = [];
      for (let s = 0; s < N; s++) {
        const C = H.series[s].C, r = arr(D);
        for (let t = p; t < D; t++) r[t] = (C[t] / C[t - p] - 1) * 100;
        R.push(r);
      }
      const W = {}, WR = {}, CN = {};
      for (let s = 0; s < N; s++) {
        const g = secs[s]; if (g == null) continue;
        if (!W[g]) { W[g] = new Float64Array(D); WR[g] = new Float64Array(D); CN[g] = new Float64Array(D); }
        for (let t = 0; t < D; t++) { const x = R[s][t]; if (isn(x)) continue; W[g][t] += w[s]; WR[g][t] += w[s] * x; CN[g][t]++; }
      }
      out[p] = [];
      for (let s = 0; s < N; s++) {
        const g = secs[s], rel = arr(D), sr = arr(D);
        if (g != null) for (let t = 0; t < D; t++) {
          const x = R[s][t]; if (isn(x) || CN[g][t] - 1 < 2 || W[g][t] - w[s] <= 0) continue;
          sr[t] = (WR[g][t] - w[s] * x) / (W[g][t] - w[s]); rel[t] = x - sr[t];
        }
        out[p].push({ rel, sr });
      }
    }
    return (H.rel = out);
  }

  function buildTest(c, fmap) {
    const f = fmap[c.field]; if (!f || f.bt === false) return null;
    const v1 = Number(c.value), v2 = Number(c.value2);
    if (c.value === null || c.value === undefined || isn(v1)) return null;
    if (f.type !== "num" || c.op === "is") return (x) => x === v1;
    switch (c.op) {
      case ">": return (x) => x > v1;
      case ">=": return (x) => x >= v1;
      case "<": return (x) => x < v1;
      case "<=": return (x) => x <= v1;
      case "between": {
        if (c.value2 === null || c.value2 === undefined || isn(v2)) return null;
        const lo = Math.min(v1, v2), hi = Math.max(v1, v2); return (x) => x >= lo && x <= hi;
      }
    }
    return null;
  }

  function summarize(rows, cost, extra) {
    const n = rows.length; if (!n) return { n: 0 };
    const r = rows.map((z) => z.r), x = rows.map((z) => z.x);
    const mean = (a) => a.reduce((s, v) => s + v, 0) / a.length;
    const sorted = Float64Array.from(r).sort();
    const out = {
      n, mean: mean(r), median: n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2,
      win: r.filter((v) => v > 0).length / n, excess: mean(x), xwin: x.filter((v) => v > 0).length / n,
      net: mean(r) - cost, best: sorted[n - 1], worst: sorted[0],
    };
    return extra ? Object.assign(out, extra(rows)) : out;
  }
  function monthlyOf(rows, dates) {
    const m = {};
    for (const z of rows) { const k = dates[z.t].slice(0, 7); (m[k] ||= { n: 0, x: 0, r: 0 }); m[k].n++; m[k].x += z.x; m[k].r += z.r; }
    return Object.entries(m).sort().map(([k, v]) => ({ m: k, n: v.n, x: v.x / v.n, r: v.r / v.n }));
  }

  /* 백테스트 1: 다음날 시가 −dip% 지정가 매수(못 사면 거래 없음)
                 → 매수일 이후 N거래일째 날, 그날 시가 +tp1% 지정가 매도(못 팔면 그날 종가)
     백테스트 2: 같은 방식으로 매수 → 매수가 대비 +tp% 익절 / −sl% 손절 / maxHold거래일째 종가 */
  function run(H, req, progress) {
    const D = H.D, N = H.meta.codes.length;
    const P = Object.assign({ dip: 2, tp1: 5, tp: 20, sl: 10, hold: 5, horizons: [1, 3, 5] }, req.params || {});
    const HZ = P.horizons.filter((h) => h >= 1 && h <= MAXK);
    const hold = Math.max(1, Math.min(MAXK, Math.round(P.hold)));
    const dip = P.dip / 100, tp1 = P.tp1 / 100, tp = P.tp / 100, sl = P.sl / 100;
    const fmap = {}; req.fields.forEach((f) => (fmap[f.key] = f));
    const F = req.filters, cost = (req.cost ?? 0.25) / 100, start = Math.min(req.warmup ?? 130, D - 25);
    const conds = [], skipped = [];
    for (const c of req.conds) {
      const t = buildTest(c, fmap);
      if (t) conds.push({ ...c, test: t, f: fmap[c.field] }); else skipped.push(fmap[c.field]?.label || c.field);
    }
    if (conds.some((c) => /^(rel\d+|sec_ret\d+)$/.test(c.field))) sectorRel(H);
    const b1 = {}; HZ.forEach((h) => (b1[h] = []));
    const b2 = [], recent = [], perDay = new Float64Array(D);
    let signals = 0, fills = 0, evaluable = 0;
    for (let s = 0; s < N; s++) {
      if (s % 50 === 0 && progress) progress(s / N);
      const code = H.meta.codes[s], mk = H.meta.market[s];
      if (!F.markets.includes(mk)) continue;
      if (F.exclude_pref && code[5] !== "0") continue;
      if (F.exclude_spac && H.meta.spac[s]) continue;
      const S = H.series[s], K = compact(S);
      if (K.c.length < 30) continue;
      const fields = computeFields(K.c, K.o, K.h, K.l, K.v);
      const getArr = (c) => {
        const key = c.field, f = c.f;
        if (f.param) { const n = Math.max(1, Math.min(120, Math.round(c.param || 20))); return paramField(key, K.c, n); }
        if (key === "marcap") return arr(K.c.length, H.meta.marcap[s] ?? NaN_);
        const m = /^(rel|sec_ret)(\d+)$/.exec(key);
        if (m) { const R = H.rel[+m[2]][s][m[1] === "rel" ? "rel" : "sr"]; return Float64Array.from(K.idx, (t) => R[t]); }
        return fields[key];
      };
      const arrs = conds.map(getArr);
      for (let i = 0; i < K.c.length; i++) {
        const t = K.idx[i];
        if (t < start || t >= D - 1) continue;
        if (!(fields.volume[i] > 0)) continue;
        if (!(fields.amount[i] >= (F.min_amount || 0))) continue;
        if (!(K.c[i] >= (F.min_price || 0))) continue;
        let ok = true;
        for (let k = 0; k < conds.length; k++) { const x = arrs[k][i]; if (isn(x) || !conds[k].test(x)) { ok = false; break; } }
        if (!ok) continue;
        const bd = t + 1, O1 = S.O[bd]; if (!(O1 > 0)) continue;   // 다음날 거래가 없으면 제외
        signals++; perDay[t]++;
        const rec = { code, t, filled: false, b1: {}, b2: null };
        recent.push(rec);
        const limit = O1 * (1 - dip);
        if (!(S.L[bd] <= limit)) continue;                          // 지정가까지 안 내려옴 → 거래 없음
        const entry = limit; fills++; rec.filled = true; rec.entry = entry;
        // ---- 백테스트 1
        for (const h of HZ) {
          const d = bd + h; if (d >= D) continue;
          const o = S.O[d], hi = S.H[d], c = S.C[d]; if (isn(o) || isn(c)) continue;
          const tgt = o * (1 + tp1), hit = hi >= tgt, px = hit ? tgt : c;
          const r = px / entry - 1;
          b1[h].push({ t, r, x: r - H.bench[h][t], hit }); rec.b1[h] = r;
        }
        // ---- 백테스트 2
        const stop = entry * (1 - sl), target = entry * (1 + tp);
        let out = null;
        if (S.L[bd] <= stop) out = { px: stop, k: 0, why: "sl" };   // 매수 당일 손절 (지정가 체결 뒤에만 더 내려갈 수 있음)
        else for (let k = 1; k <= hold; k++) {
          const d = bd + k; if (d >= D) break;
          const o = S.O[d], hi = S.H[d], lo = S.L[d], c = S.C[d];
          if (isn(o)) { if (k === hold) break; continue; }          // 거래정지일은 그냥 보유
          if (o <= stop) { out = { px: o, k, why: "sl" }; break; }  // 갭하락으로 손절선 아래 시작
          if (o >= target) { out = { px: o, k, why: "tp" }; break; } // 갭상승으로 익절선 위 시작
          if (lo <= stop) { out = { px: stop, k, why: "sl" }; break; } // 같은 날 둘 다 닿으면 손절 먼저(보수적)
          if (hi >= target) { out = { px: target, k, why: "tp" }; break; }
          if (k === hold) { out = { px: c, k, why: "exp" }; break; }
        }
        if (out) {
          const r = out.px / entry - 1;
          b2.push({ t, r, x: r - H.bench[out.k][t], why: out.why, k: out.k });
          rec.b2 = { r, why: out.why, k: out.k };
        }
      }
    }
    const stats1 = {};
    for (const h of HZ) stats1[h] = summarize(b1[h], cost, (rows) => ({ hit: rows.filter((z) => z.hit).length / rows.length }));
    const stats2 = summarize(b2, cost, (rows) => {
      const by = (w) => rows.filter((z) => z.why === w);
      const avg = (a) => (a.length ? a.reduce((s, z) => s + z.r, 0) / a.length : null);
      return {
        tp: by("tp").length / rows.length, sl: by("sl").length / rows.length, exp: by("exp").length / rows.length,
        tpAvg: avg(by("tp")), slAvg: avg(by("sl")), expAvg: avg(by("exp")),
        hold: rows.reduce((s, z) => s + z.k, 0) / rows.length,
      };
    });
    let days = 0; for (let t = start; t < D - 1; t++) if (perDay[t]) days++;
    recent.sort((a, b) => b.t - a.t);
    const lastH = HZ[HZ.length - 1];
    return {
      params: { ...P, hold, horizons: HZ }, signals, fills, days, testDays: D - 1 - start,
      period: [H.meta.dates[start], H.meta.dates[D - 2]],
      bt1: { stats: stats1, monthly: monthlyOf(b1[lastH] || [], H.meta.dates), monthlyH: lastH },
      bt2: { stats: stats2, monthly: monthlyOf(b2, H.meta.dates) },
      recent: recent.slice(0, 30).map((z) => ({ code: z.code, date: H.meta.dates[z.t], filled: z.filled, b1: z.b1, b2: z.b2 })),
      skipped, universe: N, cost: cost * 100,
    };
  }

  G.BT = { computeFields, paramField, unshuffle, stockSeries, compact, prepare, run, sectorRel };
})(typeof self !== "undefined" ? self : globalThis);
