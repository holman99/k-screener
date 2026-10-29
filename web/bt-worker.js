/* 백테스트 작업자: 무거운 계산을 화면과 따로 돌려서 휴대폰 화면이 멈추지 않게 합니다. */
importScripts("bt-engine.js");
let H = null, ver = null;

async function load(base, version) {
  if (H && ver === version) return H;
  postMessage({ type: "progress", phase: "과거 데이터 받는 중", pct: 0 });
  const meta = await (await fetch(`${base}hist.json?v=${version}`)).json();
  const resp = await fetch(`${base}hist.bin.gz?v=${version}`);
  if (!resp.ok) throw new Error("과거 데이터 파일을 받지 못했습니다 (" + resp.status + ")");
  const total = Number(resp.headers.get("content-length")) || 0;
  const reader = resp.body.getReader(), chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    postMessage({ type: "progress", phase: `과거 데이터 받는 중 (${(got / 1e6).toFixed(1)}MB${total ? " / " + (total / 1e6).toFixed(1) + "MB" : ""})`, pct: total ? Math.min(1, got / total) : 0 });
  }
  let buf = new Uint8Array(got), off = 0;
  for (const c of chunks) { buf.set(c, off); off += c.length; }
  if (buf[0] === 0x1f && buf[1] === 0x8b) {
    if (typeof DecompressionStream === "undefined") throw new Error("이 기기의 브라우저가 너무 오래되어 백테스트를 할 수 없습니다 (iOS 16.4 이상 필요).");
    const ds = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    buf = new Uint8Array(await new Response(ds).arrayBuffer());
  }
  postMessage({ type: "progress", phase: "데이터 준비 중", pct: 0 });
  H = { meta, D: meta.D, I: BT.unshuffle(buf) };
  BT.prepare(H);
  ver = version;
  return H;
}

onmessage = async (e) => {
  const { base, version, req } = e.data;
  try {
    const h = await load(base, version);
    const t0 = Date.now();
    const out = BT.run(h, req, (p) => postMessage({ type: "progress", phase: "계산 중", pct: p }));
    out.ms = Date.now() - t0;
    postMessage({ type: "result", result: out });
  } catch (err) {
    postMessage({ type: "error", message: String(err && err.message || err) });
  }
};
