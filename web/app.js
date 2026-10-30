/* K-스크리너 (웹 버전) — 모든 스크리닝 계산을 브라우저 안에서 합니다. */
const $ = (s) => document.querySelector(s);
const LS_CUR = "kscr_current_v1", LS_SAVED = "kscr_saved_v1";
const isMobile = () => window.matchMedia("(max-width:900px)").matches;

let FIELDS = [], FMAP = {}, SNAP = [], META = {}, CLOSES = null, VER = "";
let conds = [], lastResult = null, sortKey = "amount", sortAsc = false;
let chartCode = null, chartDays = 120, overlay = "ma", uid = 1;

const PRESETS = [
  { name: "정배열 + 20일선 상승 가속", c: [["ma_align_up", "is", 1], ["ma20_d1", ">", 0], ["ma20_d2", ">", 0]] },
  { name: "5·20 골든크로스 3일 이내", c: [["gc_5_20", "<=", 3]] },
  { name: "MACD 골든크로스 3일 이내", c: [["gc_macd", "<=", 3]] },
  { name: "MACD 상승 가속 (1·2차 미분 +)", c: [["macd_d1", ">", 0], ["macd_d2", ">", 0]] },
  { name: "RSI 과매도 반등", c: [["rsi", "<=", 35], ["rsi_d1", ">", 0]] },
  { name: "볼린저 상단 돌파", c: [["bb_pctb", ">", 1]] },
  { name: "볼린저 수렴 (스퀴즈)", c: [["bb_bw_rel", "<=", 1.15]] },
  { name: "일목 구름 위 + 전환>기준 + 후행 양호", c: [["cloud_pos", "is", 1], ["tk_gap", ">", 0], ["chikou", ">", 0]] },
  { name: "거래량 3배 급증", c: [["vol_ratio", ">=", 3]] },
  { name: "20일 종가 신고가", c: [["hi_n", ">", 0, null, 20]] },
  { name: "최근 20일 20% 이상 상승", c: [["ret_n", ">=", 20, null, 20]] },
  { name: "최근 5일 10% 이상 하락", c: [["ret_n", "<=", -10, null, 5]] },
];
const OPS = [[">", "보다 큼"], [">=", "이상"], ["<", "보다 작음"], ["<=", "이하"], ["between", "사이"]];

/* ---------------- 공통 ---------------- */
async function getJSON(path, nocache) {
  const r = await fetch(path + (nocache ? "?t=" + Date.now() : VER ? "?v=" + VER : ""), nocache ? { cache: "no-store" } : {});
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}
function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 2200);
}
const nf0 = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
function fmt(v, unit, type, options) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (type === "bool") return v ? "예" : "아니오";
  if (type === "enum") { const o = (options || []).find((o) => o.v === v); return o ? o.t : v; }
  if (unit === "%" || unit === "%p") return (v > 0 ? "+" : "") + nf2.format(v);
  if (["억원", "원", "주", "일", "건"].includes(unit)) return nf0.format(v);
  return nf2.format(v);
}
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------------- 탭 (모바일) ---------------- */
function setTab(t) {
  document.body.classList.toggle("tab-cond", t === "cond");
  document.body.classList.toggle("tab-res", t === "res");
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
  window.scrollTo(0, 0);
}
document.querySelector(".tabs").onclick = (e) => { const b = e.target.closest("button"); if (b) setTab(b.dataset.t); };

/* ---------------- 데이터 ---------------- */
function githubWorkflowURL() {
  const h = location.hostname;
  if (!h.endsWith(".github.io")) return null;
  const owner = h.split(".")[0], repo = location.pathname.split("/").filter(Boolean)[0] || `${owner}.github.io`;
  return `https://github.com/${owner}/${repo}/actions/workflows/update.yml`;
}
function renderStatus() {
  const m = META, d = new Date(m.base_date + "T00:00:00");
  const wd = "일월화수목금토"[d.getDay()];
  const ageDays = (Date.now() - d.getTime()) / 864e5;
  $("#status").innerHTML = (m.mode === "demo" ? '<span class="tag">데모</span>' : "") +
    `기준일 <b class="${ageDays > 5 ? "old" : ""}">${m.base_date.slice(2)} (${wd})</b> · ${nf0.format(m.count)}종목` +
    `<span class="muted"> · 갱신 ${m.updated_at.slice(5)}</span>` +
    (m.news && ["blocked", "no-endpoint", "error"].includes(m.news.domestic) ? ' <span class="tag warn">뉴스 수집 실패 · 이전 기사 사용</span>' : "");
  const u = githubWorkflowURL();
  if (u) { const b = $("#btnUpdate"); b.href = u; b.classList.remove("hidden"); }
}
async function loadData() {
  META = await getJSON("data/meta.json", true);
  VER = encodeURIComponent(META.updated_at || "");
  renderStatus();
  const [fields, snap] = await Promise.all([getJSON("data/fields.json"), getJSON("data/snapshot.json")]);
  FIELDS = fields; FIELDS.forEach((f) => (FMAP[f.key] = f));
  SNAP = snap.rows.map((r) => Object.fromEntries(snap.cols.map((c, i) => [c, r[i]])));
}
async function ensureCloses() {
  if (CLOSES) return CLOSES;
  const j = await getJSON("data/closes.json");
  CLOSES = {}; j.codes.forEach((c, i) => (CLOSES[c] = j.data[i]));
  return CLOSES;
}
function paramValue(kind, code, n) {
  const a = CLOSES[code]; if (!a || a.length <= n) return null;
  const last = a[a.length - 1]; if (last == null) return null;
  if (kind === "ret_n") { const p = a[a.length - 1 - n]; return p ? (last / p - 1) * 100 : null; }
  const win = a.slice(a.length - 1 - n, a.length - 1).filter((v) => v != null);
  if (!win.length) return null;
  if (kind === "hi_n") return (last / Math.max(...win) - 1) * 100;
  if (kind === "lo_n") return (last / Math.min(...win) - 1) * 100;
  return null;
}

/* ---------------- 조건 편집 ---------------- */
function fieldSelect(sel) {
  let h = "", g = null;
  for (const f of FIELDS) {
    if (f.group !== g) { if (g) h += "</optgroup>"; g = f.group; h += `<optgroup label="${esc(g)}">`; }
    h += `<option value="${f.key}" ${f.key === sel ? "selected" : ""}>${esc(f.label)}</option>`;
  }
  return `<select class="f" aria-label="지표">${h}</optgroup></select>`;
}
function newCond(field, op, value, value2, param) {
  const f = FMAP[field] || FIELDS[0];
  return { id: uid++, field: f.key, op: op || (f.type === "num" ? ">" : "is"),
    value: value ?? (f.type === "num" ? 0 : f.type === "enum" ? f.options[0].v : 1),
    value2: value2 ?? null, param: param ?? (f.param ? 20 : null) };
}
function renderConds() {
  const box = $("#conds");
  if (!conds.length) {
    box.innerHTML = '<div class="empty-conds">조건이 없습니다. 빠른 조건을 누르거나 [+ 조건 추가]를 누르세요.<br>조건 없이 실행하면 대상 종목 전체가 나옵니다.</div>';
    save(); return;
  }
  box.innerHTML = conds.map((c) => {
    const f = FMAP[c.field]; let bot = "";
    if (f.param) bot += `최근 <input type="number" inputmode="numeric" class="n" data-k="param" value="${c.param}" min="1" max="120">일,&nbsp;`;
    if (f.type === "bool") {
      bot += `<select data-k="value"><option value="1" ${c.value == 1 ? "selected" : ""}>예 (해당함)</option><option value="0" ${c.value == 0 ? "selected" : ""}>아니오</option></select>`;
    } else if (f.type === "enum") {
      bot += `<select data-k="value">${f.options.map((o) => `<option value="${o.v}" ${c.value == o.v ? "selected" : ""}>${esc(o.t)}</option>`).join("")}</select>`;
    } else {
      bot += `<input type="number" inputmode="decimal" step="any" data-k="value" value="${c.value ?? ""}">`;
      if (c.op === "between") bot += `~ <input type="number" inputmode="decimal" step="any" data-k="value2" value="${c.value2 ?? ""}">`;
      bot += `<span class="unit">${esc(f.unit || "")}</span>`;
      bot += `<select data-k="op">${OPS.map(([v, t]) => `<option value="${v}" ${c.op === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
    }
    return `<div class="cond" data-id="${c.id}">
      <div class="c-top">${fieldSelect(c.field)}<button class="del" aria-label="삭제">×</button></div>
      <div class="c-bot">${bot}</div>${f.desc ? `<div class="c-desc">${esc(f.desc)}</div>` : ""}</div>`;
  }).join("");
  save();
}
$("#conds").addEventListener("change", (e) => {
  const card = e.target.closest(".cond"); if (!card) return;
  const i = conds.findIndex((c) => c.id == card.dataset.id);
  if (e.target.classList.contains("f")) { conds[i] = { ...newCond(e.target.value), id: conds[i].id }; renderConds(); return; }
  const k = e.target.dataset.k; if (!k) return;
  conds[i][k] = k === "op" ? e.target.value : e.target.value === "" ? null : Number(e.target.value);
  if (k === "op") renderConds(); else save();
});
$("#conds").addEventListener("click", (e) => {
  if (!e.target.classList.contains("del")) return;
  const id = e.target.closest(".cond").dataset.id;
  conds = conds.filter((c) => c.id != id); renderConds();
});
$("#btnAdd").onclick = () => { conds.push(newCond("ma20_d1")); renderConds(); };
$("#btnClear").onclick = () => { conds = []; renderConds(); };

function renderPresets() {
  $("#presets").innerHTML = PRESETS.map((p, i) => `<button class="chip" data-i="${i}">${esc(p.name)}</button>`).join("");
  $("#presets").onclick = (e) => {
    const i = e.target.dataset.i; if (i === undefined) return;
    for (const [f, op, v, v2, n] of PRESETS[i].c) {
      const want = n ?? (FMAP[f].param ? 20 : null);
      const ex = conds.find((c) => c.field === f && (c.param ?? null) === want);
      if (ex) Object.assign(ex, { op, value: v, value2: v2 ?? null });
      else conds.push(newCond(f, op, v, v2, n));
    }
    renderConds(); toast(`'${PRESETS[i].name}' 추가됨`);
  };
}

/* ---------------- 저장 (이 기기에만 저장됨) ---------------- */
function filters() {
  return {
    markets: ["KOSPI", "KOSDAQ"].filter((m) => $("#m" + m).checked),
    min_amount: Number($("#minAmount").value) || 0, min_price: Number($("#minPrice").value) || 0,
    exclude_pref: $("#exPref").checked, exclude_spac: $("#exSpac").checked,
  };
}
const snapshotState = () => ({ conds: conds.map(({ id, ...r }) => r), filters: filters() });
function applyState(s) {
  if (!s) return;
  conds = (s.conds || []).filter((c) => FMAP[c.field]).map((c) => ({ ...c, id: uid++ }));
  const f = s.filters || {};
  if (f.markets) ["KOSPI", "KOSDAQ"].forEach((m) => ($("#m" + m).checked = f.markets.includes(m)));
  if (f.min_amount !== undefined) $("#minAmount").value = f.min_amount;
  if (f.min_price !== undefined) $("#minPrice").value = f.min_price;
  if (f.exclude_pref !== undefined) $("#exPref").checked = f.exclude_pref;
  if (f.exclude_spac !== undefined) $("#exSpac").checked = f.exclude_spac;
  renderConds();
}
function save() { try { localStorage.setItem(LS_CUR, JSON.stringify(snapshotState())); } catch (e) {} }
function savedSets() { try { return JSON.parse(localStorage.getItem(LS_SAVED) || "{}"); } catch (e) { return {}; } }
function renderSaved() {
  $("#savedSel").innerHTML = '<option value="">저장된 조건 불러오기…</option>' +
    Object.keys(savedSets()).map((k) => `<option>${esc(k)}</option>`).join("");
}
$("#btnSave").onclick = () => {
  if (!conds.length) return toast("저장할 조건이 없습니다.");
  const name = prompt("이 조건 묶음의 이름", "나의 조건 " + (Object.keys(savedSets()).length + 1));
  if (!name) return;
  const s = savedSets(); s[name] = snapshotState();
  try { localStorage.setItem(LS_SAVED, JSON.stringify(s)); } catch (e) {}
  renderSaved(); $("#savedSel").value = name; toast(`'${name}' 저장됨`);
};
$("#savedSel").onchange = (e) => { const s = savedSets()[e.target.value]; if (s) { applyState(s); toast(`'${e.target.value}' 불러옴`); } };
$("#btnDelSaved").onclick = () => {
  const k = $("#savedSel").value; if (!k) return toast("삭제할 저장 조건을 먼저 고르세요.");
  if (!confirm(`'${k}'을(를) 삭제할까요?`)) return;
  const s = savedSets(); delete s[k];
  try { localStorage.setItem(LS_SAVED, JSON.stringify(s)); } catch (e) {}
  renderSaved();
};
["#mKOSPI", "#mKOSDAQ", "#minAmount", "#minPrice", "#exPref", "#exSpac"].forEach((s) => $(s).addEventListener("change", save));

/* ---------------- 스크리닝 ---------------- */
async function run(switchTab = true) {
  const btn = $("#btnRun"); btn.disabled = true; btn.textContent = "찾는 중…";
  try {
    const F = filters();
    const needParam = conds.some((c) => FMAP[c.field]?.param);
    if (needParam) await ensureCloses();
    let rows = SNAP.filter((r) => F.markets.includes(r.market) && r.volume > 0 &&
      (!F.exclude_pref || r.code.endsWith("0")) && (!F.exclude_spac || !r.name.includes("스팩")) &&
      (r.amount ?? 0) >= F.min_amount && r.close >= F.min_price);
    const shown = [];
    for (const c of conds) {
      const f = FMAP[c.field]; if (!f) continue;
      let key = c.field, label = f.label;
      if (f.param) {
        const n = Math.max(1, Math.min(120, Math.round(c.param || 20)));
        key = c.field.replace("_n", "_" + n); label = f.label.replace("N일", n + "일");
        rows = rows.map((r) => (key in r ? r : { ...r, [key]: paramValue(c.field, r.code, n) }));
      }
      const v1 = Number(c.value), v2 = Number(c.value2);
      if (c.value === null || Number.isNaN(v1)) continue;
      let test;
      if (f.type !== "num" || c.op === "is") test = (x) => x === v1;
      else if (c.op === ">") test = (x) => x > v1;
      else if (c.op === ">=") test = (x) => x >= v1;
      else if (c.op === "<") test = (x) => x < v1;
      else if (c.op === "<=") test = (x) => x <= v1;
      else if (c.op === "between") { if (Number.isNaN(v2) || c.value2 === null) continue; const lo = Math.min(v1, v2), hi = Math.max(v1, v2); test = (x) => x >= lo && x <= hi; }
      else continue;
      rows = rows.filter((r) => r[key] !== null && r[key] !== undefined && test(r[key]));
      if (!shown.some((s) => s.key === key)) shown.push({ key, label, unit: f.unit, type: f.type, options: f.options });
    }
    lastResult = { total: rows.length, rows, columns: shown };
    renderTable();
    if (switchTab && isMobile()) setTab("res");
  } catch (e) { console.error(e); toast("데이터를 읽지 못했습니다. 새로고침해 보세요.", true); }
  finally { btn.disabled = false; btn.textContent = "스크리닝 실행"; }
}
$("#btnRun").onclick = () => run(true);

function columns() {
  const base = [
    { key: "name", label: "종목명", l: true }, { key: "close", label: "종가", unit: "원" },
    { key: "chg1", label: "등락률", unit: "%" },
    { key: "sector", label: "업종", l: true }, { key: "rel5", label: "업종 대비 5일 (%p)", unit: "%p", type: "num" },
    { key: "news48", label: "기사 48h", unit: "건", type: "num" }, { key: "news7d", label: "기사 7일", unit: "건", type: "num" },
    { key: "news_ratio", label: "기사 배수", unit: "배", type: "num" },
    { key: "amount", label: "거래대금(억)", unit: "억원" },
    { key: "marcap", label: "시총(억)", unit: "억원" }, { key: "market", label: "시장", l: true },
  ];
  const extra = lastResult.columns.filter((c) => !base.some((b) => b.key === c.key))
    .map((c) => ({ ...c, label: c.label + (c.unit && c.type === "num" ? ` (${c.unit})` : "") }));
  return base.slice(0, 3).concat(extra, base.slice(3));
}
function renderTable() {
  const r = lastResult, cols = columns();
  const ph = $("#placeholder");
  ph.classList.toggle("hidden", r.rows.length > 0);
  if (!r.rows.length) ph.innerHTML = "조건을 만족하는 종목이 없습니다.<br><span class='muted'>조건을 조금 느슨하게 바꿔 보세요.</span>";
  $("#count").textContent = nf0.format(r.total) + "개 종목";
  $("#tabCount").textContent = `(${nf0.format(r.total)})`;
  $("#countSub").textContent = r.total > 1000 ? "(상위 1,000개 표시)" : "";
  $("#btnCsv").disabled = !r.rows.length;
  const rows = [...r.rows].sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y, "ko") : x - y) * (sortAsc ? 1 : -1);
  }).slice(0, 1000);
  $("#tbl thead").innerHTML = "<tr>" + cols.map((c) =>
    `<th data-k="${c.key}" class="${c.l ? "l " : ""}${sortKey === c.key ? "sorted" + (sortAsc ? " asc" : "") : ""}">${esc(c.label)}</th>`).join("") + "</tr>";
  $("#tbl tbody").innerHTML = rows.map((row) => `<tr data-code="${row.code}">` + cols.map((c) => {
    const v = row[c.key];
    if (c.key === "name") return `<td class="l name">${esc(v)}<small>${row.code}</small></td>`;
    if (c.key === "market") return `<td class="l"><span class="mk ${v}">${v}</span></td>`;
    if (c.key === "sector") return `<td class="l sec">${v ? esc(v) : "–"}</td>`;
    if (c.key === "news7d" && row.news_cap && v != null) return `<td>${nf0.format(v)}+</td>`;
    if (c.key === "news_ratio" && v != null) return `<td class="${v >= 2 ? "hot" : ""}">${nf2.format(v)}</td>`;
    const color = (c.unit === "%" || c.unit === "%p") && c.type !== "bool" && c.type !== "enum" ? cls(v) : "";
    return `<td class="${color}">${fmt(v, c.unit, c.type, c.options)}</td>`;
  }).join("") + "</tr>").join("");
  lastResult._sorted = rows;
}
$("#tbl thead").addEventListener("click", (e) => {
  const k = e.target.dataset.k; if (!k) return;
  if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = ["name", "market"].includes(k); }
  renderTable();
});
$("#tbl tbody").addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) openChart(tr.dataset.code); });

$("#btnCsv").onclick = () => {
  const cols = [{ key: "code", label: "코드" }].concat(columns()), rows = lastResult._sorted;
  const lines = [cols.map((c) => `"${c.label}"`).join(",")].concat(rows.map((r) => cols.map((c) => {
    const v = r[c.key]; if (v == null) return "";
    return c.key === "code" ? `="${v}"` : typeof v === "string" ? `"${v.replace(/"/g, '""')}"` : v;
  }).join(",")));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `스크리닝_${META.base_date}.csv`; a.click();
};

/* ---------------- 차트용 지표 계산 ---------------- */
const sma = (a, n) => a.map((_, i) => { if (i < n - 1) return null; let s = 0; for (let k = i - n + 1; k <= i; k++) s += a[k]; return s / n; });
const ema = (a, n) => { const k = 2 / (n + 1), o = []; a.forEach((v, i) => o.push(i ? v * k + o[i - 1] * (1 - k) : v)); return o; };
const rollMax = (a, n) => a.map((_, i) => (i < n - 1 ? null : Math.max(...a.slice(i - n + 1, i + 1))));
const rollMin = (a, n) => a.map((_, i) => (i < n - 1 ? null : Math.min(...a.slice(i - n + 1, i + 1))));
function stdev(a, n) {
  return a.map((_, i) => { if (i < n - 1) return null; const w = a.slice(i - n + 1, i + 1), m = w.reduce((s, v) => s + v, 0) / n;
    return Math.sqrt(w.reduce((s, v) => s + (v - m) ** 2, 0) / n); });
}
function rsi(c, n = 14) {
  const o = Array(c.length).fill(null); let au = 0, ad = 0;
  for (let i = 1; i < c.length; i++) {
    const d = c[i] - c[i - 1], u = Math.max(d, 0), dn = Math.max(-d, 0);
    if (i <= n) { au += u / n; ad += dn / n; if (i < n) continue; }
    else { au = (au * (n - 1) + u) / n; ad = (ad * (n - 1) + dn) / n; }
    o[i] = ad === 0 ? 100 : 100 - 100 / (1 + au / ad);
  }
  return o;
}
function chartSeries(j) {
  const c = j.c, h = j.h, l = j.l, S = META.ichi_shift || 26, out = {};
  [5, 20, 60, 120].forEach((n) => (out["ma" + n] = sma(c, n)));
  const mid = sma(c, 20), sd = stdev(c, 20);
  out.bb_mid = mid; out.bb_up = mid.map((m, i) => (m == null ? null : m + 2 * sd[i])); out.bb_dn = mid.map((m, i) => (m == null ? null : m - 2 * sd[i]));
  const mid2 = (a, b) => a.map((v, i) => (v == null || b[i] == null ? null : (v + b[i]) / 2));
  const tenkan = mid2(rollMax(h, 9), rollMin(l, 9)), kijun = mid2(rollMax(h, 26), rollMin(l, 26));
  out.tenkan = tenkan; out.kijun = kijun;
  out.spanA_raw = mid2(tenkan, kijun); out.spanB_raw = mid2(rollMax(h, 52), rollMin(l, 52));
  const e26 = ema(c, 26), m = ema(c, 12).map((v, i) => v - e26[i]);
  const sig = ema(m, 9);
  out.macd = m; out.macd_signal = sig; out.macd_hist = m.map((v, i) => v - sig[i]);
  out.rsi = rsi(c);
  return out;
}

/* ---------------- 차트 ---------------- */
let view = "chart", SECTORS = null;
async function openChart(code) {
  chartCode = code; $("#modal").classList.remove("hidden"); document.body.style.overflow = "hidden";
  const inf = SNAP.find((r) => r.code === code) || {};
  $("#mTitle").innerHTML = `${esc(inf.name || code)}<span class="code">${code} · ${inf.market || ""}${inf.sector ? " · " + esc(inf.sector) : ""}</span>`;
  $("#mSub").innerHTML = `${META.base_date} 종가 <b>${inf.close != null ? nf0.format(inf.close) : "–"}원</b> <span class="${cls(inf.chg1)}">${inf.chg1 != null ? fmt(inf.chg1, "%") + "%" : ""}</span>` +
    (inf.rsi != null ? ` · RSI ${nf2.format(inf.rsi)}` : "") + (inf.vol_ratio != null ? ` · 거래량 ${nf2.format(inf.vol_ratio)}배` : "");
  showView(view);
  if (view === "news") return renderNews(code, inf);
  if (view === "sec") return renderSector(code, inf);
  let j;
  try { j = await getJSON(`data/c/${code}.json`); } catch (e) { toast("차트 데이터를 불러오지 못했습니다.", true); return; }
  // 미완성 오늘 봉은 빌드에서 이미 제외된 기준일까지만 표시
  let end = j.d.length; while (end > 0 && j.d[end - 1] > META.base_date) end--;
  ["d", "o", "h", "l", "c", "v"].forEach((k) => (j[k] = j[k].slice(0, end)));
  if (chartCode === code && view === "chart") drawChart(j, chartSeries(j));
}
function showView(v) {
  view = v;
  [...$("#view").children].forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("#chart").classList.toggle("hidden", v !== "chart");
  $("#chartCtrl").classList.toggle("hidden", v !== "chart");
  $("#newsPane").classList.toggle("hidden", v !== "news");
  $("#secPane").classList.toggle("hidden", v !== "sec");
}
$("#view").onclick = (e) => { const b = e.target.closest("button"); if (b) { view = b.dataset.v; openChart(chartCode); } };

/* ---------------- 뉴스 탭 ---------------- */
function ago(s) {
  const d = new Date(s.replace(" ", "T") + ":00+09:00"), h = (Date.now() - d) / 36e5;
  if (h < 1) return Math.max(1, Math.round(h * 60)) + "분 전";
  if (h < 24) return Math.round(h) + "시간 전";
  return s.slice(5, 10).replace("-", ".") + " " + s.slice(11, 16);
}
async function renderNews(code, inf) {
  const P = $("#newsPane");
  const naver = `https://finance.naver.com/item/news.naver?code=${code}`;
  const link = `<a class="btn ghost small" href="${naver}" target="_blank" rel="noopener">네이버 증권에서 지금 뉴스 보기 ↗</a>`;
  P.innerHTML = `<p class="muted">불러오는 중…</p>`;
  let j;
  try { j = await getJSON(`data/news/${code}.json`); }
  catch (e) {
    P.innerHTML = `<div class="note">이 종목은 뉴스 집계 대상이 아닙니다 (거래대금 5억원 이상 종목만 모읍니다).</div>${link}`; return;
  }
  if (chartCode !== code || view !== "news") return;
  const dom = j.items.filter((i) => !i.g), glob = j.items.filter((i) => i.g);
  const ratio = inf.news_ratio;
  const card = (label, v, sub) => `<div class="stat"><div class="k">${label}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  const n7 = inf.news7d != null ? nf0.format(inf.news7d) + (j.capped ? "+" : "") : "–";
  let html = `<div class="stats">
    ${card("국내 48시간", inf.news48 != null ? nf0.format(inf.news48) + "건" : "–")}
    ${card("국내 7일", n7 === "–" ? n7 : n7 + "건")}
    ${card("평소 대비", ratio != null ? nf2.format(ratio) + "배" : "–", ratio != null ? (ratio >= 2 ? "평소보다 많이 언급" : ratio <= 0.5 ? "평소보다 조용함" : "평소 수준") : "7일 3건 미만")}
    ${card("해외(영문) 7일", inf.gnews7d != null ? nf0.format(inf.gnews7d) + "건" : "–", inf.gnews7d == null ? "시총 상위만 집계" : j.eng ? esc(j.eng) : "")}
  </div>
  <div class="note">기사 수집 시각 ${j.fetched_at ? j.fetched_at.replace("T", " ") : "–"} · 하루 3번(08·16·20시) 갱신 · 배수 = (48시간 ÷ 2) ÷ (7일 ÷ 7)</div>
  ${link}`;
  const list = (arr, title) => !arr.length ? "" : `<h4>${title}</h4><ul class="news">` + arr.map((i) =>
    `<li><a href="${esc(i.u || naver)}" target="_blank" rel="noopener"><span class="nt">${esc(i.t)}</span><span class="nm">${esc(i.s || "")} · ${ago(i.d)}</span></a></li>`).join("") + "</ul>";
  html += list(dom, `국내 기사 (최근 ${dom.length}건)`) + list(glob, "해외(영문) 기사");
  if (!dom.length && !glob.length) html += `<div class="note">최근 7일 동안 수집된 기사가 없습니다.</div>`;
  P.innerHTML = html;
}

/* ---------------- 업종 탭 ---------------- */
async function renderSector(code, inf) {
  const P = $("#secPane");
  if (!inf.sector) { P.innerHTML = `<div class="note">업종 분류 정보가 없는 종목입니다.</div>`; return; }
  if (!SECTORS) { try { SECTORS = await getJSON("data/sectors.json"); } catch (e) { SECTORS = {}; } }
  if (chartCode !== code || view !== "sec") return;
  const S = SECTORS[inf.sector] || {};
  const pct = (v) => (v == null ? "–" : `<span class="${cls(v)}">${fmt(v, "%")}%</span>`);
  const pp = (v) => (v == null ? "–" : `<span class="${cls(v)}">${fmt(v, "%p")}%p</span>`);
  const row = (p, lab) => `<tr><td class="l">${lab}</td><td>${pct(inf["ret" + p])}</td><td>${pct(inf["sec_ret" + p])}</td><td><b>${pp(inf["rel" + p])}</b></td></tr>`;
  const peers = (S.members || []).map((c) => SNAP.find((r) => r.code === c)).filter(Boolean)
    .sort((a, b) => (b.ret5 ?? -1e9) - (a.ret5 ?? -1e9));
  const rank = peers.findIndex((r) => r.code === code) + 1;
  P.innerHTML = `
    <div class="note"><b>${esc(inf.sector)}</b> 업종 ${peers.length}종목 · 업종 등락률은 같은 업종의 다른 종목들을 시가총액으로 가중 평균한 값입니다 (자기 자신 제외).
    분류: ${({ "naver-app": "네이버 증권 업종", "naver-pc": "네이버 증권 업종", "kind": "거래소 표준산업분류", demo: "데모" })[String(META.sector_source || "").replace("(이전)", "")] || "알 수 없음"}</div>
    <table class="mini"><thead><tr><th class="l">기간</th><th>이 종목</th><th>업종</th><th>차이</th></tr></thead>
    <tbody>${row(2, "2거래일 (≈48시간)")}${row(5, "5거래일 (≈1주)")}${row(20, "20거래일 (≈1달)")}</tbody></table>
    ${rank ? `<p class="muted">5거래일 등락률 기준 업종 내 ${rank}위 / ${peers.length}종목</p>` : ""}
    <h4>같은 업종 종목 (5거래일 등락률 순)</h4>
    <table class="mini peers"><thead><tr><th class="l">종목</th><th>2일</th><th>5일</th><th>20일</th></tr></thead><tbody>
    ${peers.slice(0, 40).map((r) => `<tr class="${r.code === code ? "me" : ""}" data-code="${r.code}"><td class="l">${esc(r.name)}</td><td>${pct(r.ret2)}</td><td>${pct(r.ret5)}</td><td>${pct(r.ret20)}</td></tr>`).join("")}
    </tbody></table>`;
}
$("#secPane").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-code]"); if (tr && tr.dataset.code !== chartCode) { view = "chart"; openChart(tr.dataset.code); } });
function drawChart(j, s) {
  if (typeof Plotly === "undefined") { $("#chart").innerHTML = "<p style='padding:20px'>차트 도구를 불러오는 중입니다. 잠시 후 다시 눌러 주세요.</p>"; return; }
  const N = j.c.length, D = Math.min(chartDays, N), S = META.ichi_shift || 26, from = N - D;
  const cut = (a) => a.slice(from);
  const x = cut(j.d);
  const fut = []; let t = new Date(j.d[N - 1] + "T00:00:00");
  while (fut.length < S) { t = new Date(t.getTime() + 864e5); if (t.getDay() % 6) fut.push(t.toISOString().slice(0, 10)); }
  const xf = x.concat(fut);
  const shifted = (raw) => cut(raw.map((_, i) => (i - S >= 0 ? raw[i - S] : null))).concat(raw.slice(N - S));
  const UP = "#d7263d", DN = "#1d5fd1", mob = isMobile();
  const line = (y, name, color, xs = x, extra = {}) => ({ type: "scatter", mode: "lines", x: xs, y, name, line: { color, width: 1.3 }, hovertemplate: "%{y:,.0f}", ...extra });
  const tr = [{ type: "candlestick", x, open: cut(j.o), high: cut(j.h), low: cut(j.l), close: cut(j.c), name: "주가",
    increasing: { line: { color: UP, width: 1 }, fillcolor: UP }, decreasing: { line: { color: DN, width: 1 }, fillcolor: DN } }];
  if (overlay === "ma") tr.push(line(cut(s.ma5), "5", "#f59e0b"), line(cut(s.ma20), "20", "#d946ef"), line(cut(s.ma60), "60", "#10b981"), line(cut(s.ma120), "120", "#6b7280"));
  else if (overlay === "bb") tr.push(line(cut(s.bb_up), "상단", "#94a3b8"), line(cut(s.bb_dn), "하단", "#94a3b8", x, { fill: "tonexty", fillcolor: "rgba(148,163,184,.12)" }), line(cut(s.bb_mid), "중심", "#d946ef", x, { line: { color: "#d946ef", width: 1, dash: "dot" } }));
  else {
    tr.push(line(shifted(s.spanB_raw), "선행B", "#2563eb", xf, { line: { color: "#2563eb", width: 0.8 } }));
    tr.push(line(shifted(s.spanA_raw), "선행A", "#dc2626", xf, { line: { color: "#dc2626", width: 0.8 }, fill: "tonexty", fillcolor: "rgba(120,120,140,.14)" }));
    const chikou = j.c.map((_, i) => (i + S < N ? j.c[i + S] : null));
    tr.push(line(cut(s.tenkan), "전환", "#f59e0b"), line(cut(s.kijun), "기준", "#7c3aed"), line(cut(chikou), "후행", "#10b981", x, { line: { color: "#10b981", width: 1, dash: "dot" } }));
  }
  tr.push({ type: "bar", x, y: cut(j.v), name: "거래량", marker: { color: cut(j.c).map((c, i) => (c >= cut(j.o)[i] ? UP : DN)), opacity: 0.55 }, yaxis: "y2", hovertemplate: "%{y:,.0f}", showlegend: false });
  const hist = cut(s.macd_hist);
  tr.push({ type: "bar", x, y: hist, name: "히스토그램", marker: { color: hist.map((v) => (v >= 0 ? UP : DN)), opacity: 0.45 }, yaxis: "y3", hovertemplate: "%{y:,.1f}", showlegend: false });
  tr.push(line(cut(s.macd), "MACD", "#0f766e", x, { yaxis: "y3", hovertemplate: "%{y:,.1f}", showlegend: false }), line(cut(s.macd_signal), "시그널", "#f59e0b", x, { yaxis: "y3", hovertemplate: "%{y:,.1f}", showlegend: false }));
  tr.push(line(cut(s.rsi), "RSI", "#7c3aed", x, { yaxis: "y4", hovertemplate: "%{y:.1f}", showlegend: false }));
  const ax = (domain, title) => ({ domain, title: { text: title, font: { size: 10, color: "#8a90a0" }, standoff: 4 }, gridcolor: "#f0f1f4", zeroline: false, tickfont: { size: 10 }, side: "right", fixedrange: mob });
  Plotly.react("chart", tr, {
    margin: { l: 6, r: mob ? 46 : 60, t: 6, b: 26 }, showlegend: true,
    legend: { orientation: "h", y: 1.0, x: 0, yanchor: "bottom", font: { size: 10 }, bgcolor: "rgba(255,255,255,.7)" },
    hovermode: "x unified", dragmode: mob ? false : "pan", plot_bgcolor: "#fff", paper_bgcolor: "#fff",
    font: { family: "Pretendard, -apple-system, sans-serif" },
    xaxis: { type: "category", anchor: "y4", nticks: mob ? 5 : 10, showgrid: false, rangeslider: { visible: false }, tickfont: { size: 10 }, fixedrange: mob },
    yaxis: { ...ax([0.44, 0.96], ""), tickformat: ",.0f" },
    yaxis2: ax([0.31, 0.42], "거래량"), yaxis3: ax([0.16, 0.29], "MACD"),
    yaxis4: { ...ax([0, 0.14], "RSI"), range: [0, 100], tickvals: [30, 70] },
    shapes: [30, 70].map((v) => ({ type: "line", xref: "paper", x0: 0, x1: 1, yref: "y4", y0: v, y1: v, line: { color: "#cbd5e1", width: 1, dash: "dot" } })),
  }, { responsive: true, displaylogo: false, displayModeBar: !mob, scrollZoom: !mob, modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"] });
}
function closeChart() { $("#modal").classList.add("hidden"); document.body.style.overflow = ""; }
function moveChart(step) {
  const rows = lastResult?._sorted; if (!rows) return;
  const i = rows.findIndex((r) => r.code === chartCode) + step;
  if (i >= 0 && i < rows.length) openChart(rows[i].code);
}
$("#mClose").onclick = closeChart;
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeChart(); };
$("#mPrev").onclick = () => moveChart(-1);
$("#mNext").onclick = () => moveChart(1);
document.addEventListener("keydown", (e) => {
  if ($("#modal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeChart();
  if (e.key === "ArrowLeft") moveChart(-1);
  if (e.key === "ArrowRight") moveChart(1);
});
const segClick = (sel, fn) => ($(sel).onclick = (e) => { const b = e.target.closest("button"); if (!b) return; [...$(sel).children].forEach((x) => x.classList.toggle("on", x === b)); fn(b); openChart(chartCode); });
segClick("#ovl", (b) => (overlay = b.dataset.k));
segClick("#period", (b) => (chartDays = +b.dataset.d));

/* ---------------- 백테스트 ---------------- */
let worker = null, btBusy = false;
function closeBt() { $("#btModal").classList.add("hidden"); document.body.style.overflow = ""; }
$("#btClose").onclick = closeBt;
$("#btModal").onclick = (e) => { if (e.target.id === "btModal") closeBt(); };

function condText(c) {
  const f = FMAP[c.field]; if (!f) return "";
  let lab = f.param ? f.label.replace("N일", (c.param || 20) + "일") : f.label;
  if (f.type === "bool") return `${lab}: ${c.value == 1 ? "예" : "아니오"}`;
  if (f.type === "enum") return `${lab}: ${(f.options.find((o) => o.v == c.value) || {}).t}`;
  const op = { ">": ">", ">=": "≥", "<": "<", "<=": "≤" }[c.op];
  return c.op === "between" ? `${lab}: ${c.value} ~ ${c.value2}${f.unit || ""}` : `${lab} ${op} ${c.value}${f.unit || ""}`;
}

const LS_BT = "kscr_bt_v1";
const BT_DEFAULT = { dip: 2, tp1: 5, tp: 20, sl: 10, hold: 5 };
let btParams = (() => { try { return { ...BT_DEFAULT, ...JSON.parse(localStorage.getItem(LS_BT) || "{}") }; } catch (e) { return { ...BT_DEFAULT }; } })();
let btTab = "1", lastBt = null;

function startBacktest() {
  if (btBusy) return;
  if (!META.hist) return toast("백테스트 데이터가 아직 없습니다. 다음 갱신 후 이용하세요.", true);
  $("#btModal").classList.remove("hidden"); document.body.style.overflow = "hidden";
  const F = filters();
  $("#btSub").innerHTML = (conds.length ? conds.map((c) => esc(condText(c))).join(" · ") : "조건 없음 (대상 종목 전체)") +
    `<br><span class="muted">거래대금 ${F.min_amount}억↑ · 주가 ${nf0.format(F.min_price)}원↑ · ${F.markets.join("/")}</span>`;
  const mb = META.hist.bytes ? (META.hist.bytes / 1e6).toFixed(1) : "?";
  $("#btBody").innerHTML = `<div class="bt-wait"><div class="prog"><div class="bar" id="btBar"></div></div><p id="btPhase">준비 중…</p>
    <p class="muted">처음에는 과거 1년여치 주가(약 ${mb}MB)를 받습니다. 와이파이를 권장합니다. 두 번째부터는 빠릅니다.</p></div>`;
  if (!worker) worker = new Worker("bt-worker.js");
  btBusy = true;
  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === "progress") {
      const bar = $("#btBar"); if (bar) bar.style.width = Math.round((m.pct || 0) * 100) + "%";
      const ph = $("#btPhase"); if (ph) ph.textContent = m.phase;
    } else if (m.type === "result") { btBusy = false; lastBt = m.result; renderBacktest(); }
    else if (m.type === "error") { btBusy = false; $("#btBody").innerHTML = `<div class="note err">${esc(m.message)}</div>`; }
  };
  worker.onerror = (e) => { btBusy = false; $("#btBody").innerHTML = `<div class="note err">백테스트 중 오류가 났습니다: ${esc(e.message || "")}</div>`; };
  worker.postMessage({
    base: new URL("data/", location.href).href, version: META.hist.version,
    req: { conds: conds.map(({ id, ...r }) => r), filters: F, fields: FIELDS, cost: 0.25, params: { ...btParams, horizons: [1, 3, 5] } },
  });
}
$("#btnBt").onclick = startBacktest;

function renderBacktest() {
  const R = lastBt, P = R.params;
  const p = (v, d = 2) => (v == null || Number.isNaN(v) ? "–" : `<span class="${cls(v)}">${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%</span>`);
  const pc = (v) => (v == null || Number.isNaN(v) ? "–" : (v * 100).toFixed(1) + "%");
  const names = Object.fromEntries(SNAP.map((r) => [r.code, r.name]));
  const s1 = R.bt1.stats[5] || R.bt1.stats[P.horizons[P.horizons.length - 1]] || {}, s2 = R.bt2.stats || {};
  const fillRate = R.signals ? R.fills / R.signals : null;
  const inp = (k, w = 52) => `<input type="number" inputmode="decimal" step="any" data-p="${k}" value="${btParams[k]}" style="width:${w}px">`;

  // 요약 비교
  const card = (title, s, sub) => `<div class="btcard"><div class="bt-t">${title}</div>
    ${s.n ? `<div class="bt-big">${p(s.net)}</div><div class="bt-s">비용 차감 평균 수익 · ${nf0.format(s.n)}건</div>
    <div class="bt-row"><span>시장 대비</span><b>${p(s.excess)}</b></div><div class="bt-row"><span>승률</span><b>${pc(s.win)}</b></div>${sub || ""}`
    : `<div class="bt-s">체결된 거래가 없습니다</div>`}</div>`;
  let verdict;
  if (!R.signals) verdict = "이 조건에 걸린 과거 사례가 없습니다. 조건을 조금 느슨하게 해 보세요.";
  else if (!R.fills) verdict = `신호 ${nf0.format(R.signals)}건 중 시가 −${P.dip}% 지정가가 체결된 경우가 없습니다. 지정가 폭을 줄여 보세요.`;
  else {
    const better = (s1.net ?? -1e9) >= (s2.net ?? -1e9) ? "백테스트 1" : "백테스트 2";
    verdict = `신호 ${nf0.format(R.signals)}건 중 <b>${nf0.format(R.fills)}건(${pc(fillRate)})</b>이 시가 −${P.dip}%에 체결됐습니다. ` +
      `비용을 뺀 평균 수익은 백테스트 1(5거래일) <b>${p(s1.net)}</b>, 백테스트 2 <b>${p(s2.net)}</b>로 <b>${better}</b> 쪽이 나았습니다.`;
    const n = Math.min(s1.n || 0, s2.n || 0);
    if (n < 100) verdict += ` <br><b>체결 거래가 ${n}건으로 적어</b> 우연의 영향이 클 수 있습니다.`;
  }
  const monthBars = (list) => {
    if (!list.length) return "";
    const maxAbs = Math.max(0.005, ...list.map((m) => Math.abs(m.x)));
    return `<div class="bars">${list.map((m) => `<div class="brow"><span class="bm">${m.m.slice(2).replace("-", ".")}</span>
      <span class="btrack"><span class="bfill ${m.x >= 0 ? "pos" : "neg"}" style="${m.x >= 0 ? "left:50%" : "right:50%"};width:${(Math.abs(m.x) / maxAbs) * 50}%"></span></span>
      <span class="bv ${cls(m.x)}">${m.x > 0 ? "+" : ""}${(m.x * 100).toFixed(2)}%</span><span class="bn">${nf0.format(m.n)}건</span></div>`).join("")}</div>`;
  };

  // 백테스트 1 상세
  const H = [[1, "다음날"], [3, "3거래일"], [5, "5거래일"]];
  const d1 = `<div class="rule"><b>매수</b> 신호 다음 날 <b>시가 −${P.dip}%</b> 지정가 (저가가 닿지 않으면 거래 없음)<br>
    <b>매도</b> 매수일 이후 1·3·5번째 거래일에 <b>그날 시가 +${P.tp1}%</b> 지정가, 닿지 않으면 그날 종가</div>
    <div class="tbl-x"><table class="mini bt"><thead><tr><th class="l">매도일</th><th>거래 수</th><th>평균 수익</th><th>중앙값</th><th>승률</th><th>+${P.tp1}% 체결</th><th>시장 대비</th><th>비용 차감</th></tr></thead><tbody>
    ${H.map(([h, lab]) => { const s = R.bt1.stats[h] || {}; return s.n ? `<tr><td class="l">${lab}</td><td>${nf0.format(s.n)}</td><td>${p(s.mean)}</td><td>${p(s.median)}</td><td>${pc(s.win)}</td><td>${pc(s.hit)}</td><td><b>${p(s.excess)}</b></td><td>${p(s.net)}</td></tr>` : `<tr><td class="l">${lab}</td><td colspan="7" class="muted">사례 없음</td></tr>`; }).join("")}
    </tbody></table></div>
    ${R.bt1.monthly.length ? `<h4>월별 시장 대비 성과 (${R.bt1.monthlyH}거래일 매도 기준)</h4>${monthBars(R.bt1.monthly)}` : ""}`;

  // 백테스트 2 상세
  const seg = (v, c) => (v > 0 ? `<span class="${c}" style="width:${v * 100}%"></span>` : "");
  const d2 = `<div class="rule"><b>매수</b> 신호 다음 날 <b>시가 −${P.dip}%</b> 지정가 (저가가 닿지 않으면 거래 없음)<br>
    <b>매도</b> 매수가 대비 <b>+${P.tp}% 익절</b> · <b>−${P.sl}% 손절</b> · 둘 다 아니면 매수일 이후 <b>${P.hold}번째 거래일 종가</b></div>
    ${s2.n ? `<div class="tbl-x"><table class="mini bt"><thead><tr><th class="l"></th><th>거래 수</th><th>평균 수익</th><th>중앙값</th><th>승률</th><th>시장 대비</th><th>비용 차감</th><th>평균 보유</th></tr></thead><tbody>
    <tr><td class="l">전체</td><td>${nf0.format(s2.n)}</td><td>${p(s2.mean)}</td><td>${p(s2.median)}</td><td>${pc(s2.win)}</td><td><b>${p(s2.excess)}</b></td><td>${p(s2.net)}</td><td>${s2.hold.toFixed(1)}일</td></tr>
    </tbody></table></div>
    <h4>어떻게 끝났나</h4>
    <div class="stack">${seg(s2.tp, "s-tp")}${seg(s2.exp, "s-exp")}${seg(s2.sl, "s-sl")}</div>
    <div class="legend3">
      <div><span class="dot s-tp"></span>익절 <b>${pc(s2.tp)}</b> <span class="muted">평균 ${p(s2.tpAvg, 1)}</span></div>
      <div><span class="dot s-exp"></span>기간 만료 <b>${pc(s2.exp)}</b> <span class="muted">평균 ${p(s2.expAvg, 1)}</span></div>
      <div><span class="dot s-sl"></span>손절 <b>${pc(s2.sl)}</b> <span class="muted">평균 ${p(s2.slAvg, 1)}</span></div>
    </div>
    <p class="muted small">손절 평균이 −${P.sl}%보다 나쁘면 갭하락(시가부터 손절선 아래)으로 판 경우가 섞여 있다는 뜻입니다.</p>
    ${R.bt2.monthly.length ? `<h4>월별 시장 대비 성과</h4>${monthBars(R.bt2.monthly)}` : ""}` : `<div class="note">체결된 거래가 없습니다.</div>`}`;

  const why = { tp: "익절", sl: "손절", exp: "만료" };
  const recent = R.recent.length ? `<h4>최근 신호</h4><div class="tbl-x"><table class="mini peers"><thead><tr><th class="l">신호일</th><th class="l">종목</th><th>체결</th>
    <th>BT1 다음날</th><th>BT1 3일</th><th>BT1 5일</th><th>BT2 결과</th></tr></thead><tbody>
    ${R.recent.slice(0, 20).map((z) => `<tr data-code="${z.code}"><td class="l">${z.date.slice(5)}</td><td class="l">${esc(names[z.code] || z.code)}</td>
      <td>${z.filled ? "○" : '<span class="muted">미체결</span>'}</td>
      <td>${p(z.b1[1] ?? null, 1)}</td><td>${p(z.b1[3] ?? null, 1)}</td><td>${p(z.b1[5] ?? null, 1)}</td>
      <td>${z.b2 ? `${p(z.b2.r, 1)} <span class="muted">${why[z.b2.why]} ${z.b2.k}일</span>` : (z.filled ? '<span class="muted">진행 중</span>' : "–")}</td></tr>`).join("")}
    </tbody></table></div>` : "";

  $("#btBody").innerHTML = `
    <details class="btset"><summary>매매 규칙 바꾸기</summary>
      <div class="btset-grid">
        <label>매수 지정가: 다음날 시가 −${inp("dip")}%</label>
        <label>백테스트 1 매도: 그날 시가 +${inp("tp1")}%</label>
        <label>백테스트 2: 익절 +${inp("tp")}% · 손절 −${inp("sl")}% · 최대 ${inp("hold", 44)}거래일</label>
      </div>
      <div class="row gap"><button class="btn primary small" id="btRerun">이 규칙으로 다시 계산</button><button class="btn ghost small" id="btReset">처음 값으로</button></div>
    </details>
    <div class="verdict">${verdict}</div>
    ${R.skipped.length ? `<div class="note warn">과거 기록이 없어 백테스트에서 뺀 조건: ${R.skipped.map(esc).join(", ")}</div>` : ""}
    <div class="btcards">${card("백테스트 1 · 5거래일 매도", s1, `<div class="bt-row"><span>+${P.tp1}% 체결</span><b>${pc(s1.hit)}</b></div>`)}
      ${card(`백테스트 2 · 익절/손절`, s2, s2.n ? `<div class="bt-row"><span>익절 / 손절</span><b>${pc(s2.tp)} / ${pc(s2.sl)}</b></div>` : "")}</div>
    <div class="note">검증 기간 ${R.period[0]} ~ ${R.period[1]} (${R.testDays}거래일) · 대상 ${nf0.format(R.universe)}종목 ·
      신호 ${nf0.format(R.signals)}건 · 지정가 체결 ${nf0.format(R.fills)}건 (${pc(fillRate)}) · 계산 ${(R.ms / 1000).toFixed(1)}초<br>
      시장 대비 = 같은 매수일 시가에 전체 종목을 사서 같은 매도일 종가에 팔았을 때 평균과의 차이</div>
    <div class="seg bt-tabs" id="btTabs"><button data-t="1" class="${btTab === "1" ? "on" : ""}">백테스트 1 · 지정가 단기</button><button data-t="2" class="${btTab === "2" ? "on" : ""}">백테스트 2 · 익절/손절</button></div>
    <div id="btDetail">${btTab === "1" ? d1 : d2}</div>
    ${recent}
    <h4>꼭 알아 둘 점</h4>
    <ul class="caveats">
      <li>지정가는 그날 저가(또는 고가)가 그 가격에 <b>닿기만 하면 체결</b>된 것으로 봅니다. 실제로는 딱 닿은 경우 체결되지 않을 수도 있습니다.</li>
      <li>하루 안에서 고가와 저가 중 무엇이 먼저였는지는 일봉으로 알 수 없어서, 익절과 손절이 같은 날 모두 가능하면 <b>손절</b>로 계산합니다. 매수 당일은 손절만 확인합니다.</li>
      <li>시가부터 손절선 아래(갭하락)나 익절선 위(갭상승)로 시작하면 시가에 판 것으로 계산합니다.</li>
      <li>현재 상장된 종목만 들어 있어 결과가 실제보다 좋게 나올 수 있고, 기간이 약 1년으로 짧습니다.</li>
      <li>비용 ${R.cost}%(매도 거래세 0.20% + 수수료)를 '비용 차감'에 반영했습니다. 호가 단위와 체결 수량 한계는 무시했습니다.</li>
      <li>규칙과 조건을 여러 번 바꿔 가장 좋은 결과만 고르면 과거에만 맞춘 전략(과최적화)이 되기 쉽습니다.</li>
    </ul>`;
  $("#btTabs").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; btTab = b.dataset.t; renderBacktest(); };
  $("#btRerun").onclick = () => {
    document.querySelectorAll("#btBody input[data-p]").forEach((i) => { const v = Number(i.value); if (!Number.isNaN(v) && v > 0) btParams[i.dataset.p] = v; });
    btParams.hold = Math.max(1, Math.min(10, Math.round(btParams.hold)));
    try { localStorage.setItem(LS_BT, JSON.stringify(btParams)); } catch (e) {}
    startBacktest();
  };
  $("#btReset").onclick = () => { btParams = { ...BT_DEFAULT }; try { localStorage.removeItem(LS_BT); } catch (e) {} startBacktest(); };
}
$("#btBody").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-code]"); if (tr) { closeBt(); view = "chart"; openChart(tr.dataset.code); } });

/* ---------------- 시작 ---------------- */
(async function init() {
  try { await loadData(); }
  catch (e) { console.error(e); $("#status").textContent = "아직 데이터가 없습니다. 첫 갱신이 끝날 때까지 기다려 주세요 (약 10~30분)."; return; }
  $("#btnBt").disabled = false;
  renderPresets(); renderSaved();
  let cur = null; try { cur = JSON.parse(localStorage.getItem(LS_CUR) || "null"); } catch (e) {}
  if (cur) applyState(cur); else renderConds();
  run(false);
})();
