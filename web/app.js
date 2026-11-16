"use strict";
/* 정적 사이트 프런트엔드.
   - 점수·뉴스: data/*.json (GitHub Actions 가 주기적으로 갱신) 을 다시 읽는다.
   - 주가: 사용자의 Finnhub 키로 브라우저가 직접 REST(초기값) + WebSocket(체결) 을 받는다.
   - 이슈: 키가 있으면 1분마다 Finnhub 시장 뉴스를 직접 확인해 수집본과 합친다. */

const FH = "https://finnhub.io/api/v1";
const PAGE = 50;                 // Finnhub 무료 웹소켓 구독 한도와 같다
const KEY_NAME = "finnhub_key";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const S = {
  scores: null, bySymbol: new Map(), news: [], liveNews: [], newsMeta: null,
  key: "", quotes: new Map(), page: 0, q: "", sector: "", tab: "rank",
  ws: null, wsOpen: false, subs: new Set(), visible: [], quoteRun: 0,
  seenNews: new Set(), unseen: 0, firstNewsLoad: true, detail: null,
};
try { S.key = localStorage.getItem(KEY_NAME) || ""; } catch { /* 저장소 차단 환경 */ }

// ---------- 유틸 ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");
const fmt = (n, d = 2) => (n == null || !isFinite(n) ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const sign = (n, d = 2, suffix = "") => (n == null || !isFinite(n) ? "—" : (n > 0 ? "+" : "") + fmt(n, d) + suffix);
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
const kst = (iso) => {
  if (!iso) return "—";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  return isNaN(d) ? "—" : d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
};
const ago = (iso) => {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (!isFinite(m)) return "";
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  if (m < 1440) return `${Math.floor(m / 60)}시간 전`;
  return `${Math.floor(m / 1440)}일 전`;
};
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}
const fh = (path, params = {}) => getJSON(`${FH}${path}?${new URLSearchParams({ ...params, token: S.key })}`);

// ---------- 장 상태 (공휴일은 반영하지 않는다) ----------
function marketState() {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false })
    .formatToParts(new Date()).map((x) => [x.type, x.value]));
  const mins = (parseInt(p.hour, 10) % 24) * 60 + parseInt(p.minute, 10);
  const weekday = !["Sat", "Sun"].includes(p.weekday);
  if (weekday && mins >= 570 && mins < 960) return ["정규장", true];
  if (weekday && mins >= 240 && mins < 570) return ["프리마켓", false];
  if (weekday && mins >= 960 && mins < 1200) return ["애프터마켓", false];
  return ["휴장", false];
}
function renderMarket() {
  const [label, open] = marketState();
  const el = $("#market");
  el.textContent = `미국 ${label}`;
  el.classList.toggle("open", open);
}

// ---------- 데이터 로드 ----------
async function loadScores() {
  try {
    const d = await getJSON(`data/scores.json?t=${Date.now()}`);
    if (S.scores && S.scores.generated_at === d.generated_at) return;
    S.scores = d;
    S.bySymbol = new Map(d.stocks.map((s) => [s.symbol, s]));
    for (const s of d.stocks) {
      // 수집 시점 가격은 더 새로운 실시간 값이 없을 때만 쓴다
      if (s.quote && !S.quotes.get(s.symbol)?.live) S.quotes.set(s.symbol, { ...s.quote, live: false });
    }
    const sel = $("#sector"), cur = sel.value;
    const sectors = [...new Set(d.stocks.map((s) => s.sector))].sort();
    sel.innerHTML = `<option value="">전체 섹터</option>` + sectors.map((x) => `<option>${esc(x)}</option>`).join("");
    sel.value = sectors.includes(cur) ? cur : "";
    $("#sampleBanner").hidden = !d.sample;
    renderRank(); renderWeights();
    if (S.detail) openDetail(S.detail, true);
  } catch (e) {
    if (!S.scores) { $("#empty").hidden = false; $("#empty").textContent = "점수 데이터를 아직 불러오지 못했습니다. 데이터 수집(GitHub Actions)이 한 번 돌았는지 확인하세요."; }
  }
}

async function loadNews() {
  try {
    const d = await getJSON(`data/news.json?t=${Date.now()}`);
    S.news = d.items || []; S.newsMeta = d;
  } catch { /* 다음 주기에 재시도 */ }
  if (S.key) {
    try {
      const raw = await fh("/news", { category: "general" });
      S.liveNews = (raw || []).filter((n) => n.headline && n.url).map((n) => ({
        id: "fh" + n.id, title: n.headline, summary: (n.summary || "").slice(0, 280), url: n.url,
        source: n.source || "Finnhub", published: new Date(n.datetime * 1000).toISOString(), tickers: tagTickers(n.headline),
      }));
      S.liveNewsAt = new Date().toISOString();
    } catch { /* 한도 초과 등 — 수집본만 표시 */ }
  }
  renderNews();
}

// 브라우저에서 받은 헤드라인용 간단 매핑: $TICKER, (TICKER), 한 단어 회사명
function tagTickers(text) {
  if (!S.scores) return [];
  const out = new Set();
  for (const m of text.matchAll(/\$([A-Z]{1,5})\b|\(([A-Z]{1,5})\)/g)) { const t = m[1] || m[2]; if (S.bySymbol.has(t)) out.add(t); }
  return [...out];
}

function mergedNews() {
  const norm = (t) => t.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const seen = new Set(), out = [];
  for (const n of [...S.news, ...S.liveNews]) {
    const k = norm(n.title);
    if (seen.has(k)) continue;
    seen.add(k); out.push(n);
  }
  return out.sort((a, b) => (a.published < b.published ? 1 : -1));
}

// ---------- 순위 표 ----------
function filtered() {
  if (!S.scores) return [];
  const q = S.q.trim().toLowerCase();
  return S.scores.stocks.filter((s) => (!S.sector || s.sector === S.sector) && (!q || s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q)));
}

function renderRank() {
  const all = filtered();
  const pages = Math.max(1, Math.ceil(all.length / PAGE));
  S.page = Math.min(S.page, pages - 1);
  const rows = all.slice(S.page * PAGE, (S.page + 1) * PAGE);
  const rankOf = new Map(S.scores ? S.scores.stocks.map((s, i) => [s.symbol, i + 1]) : []);
  $("#rank tbody").innerHTML = rows.map((s) => {
    const delta = s.score != null && s.prev_score != null ? s.score - s.prev_score : null;
    const comp = (id) => (s.components[id] ? fmt(s.components[id].score, 0) : "—");
    return `<tr data-sym="${esc(s.symbol)}">
      <td class="num muted">${rankOf.get(s.symbol)}</td>
      <td><div class="sym">${esc(s.symbol)}</div><div class="name">${esc(s.name)}</div></td>
      <td class="hide-s muted">${esc(s.sector)}</td>
      <td class="num"><span class="score">${s.score == null ? "—" : fmt(s.score, 1)}</span><span class="scorebar"><i style="width:${s.score || 0}%"></i></span></td>
      <td class="num ${cls(delta)}">${delta == null ? "—" : sign(delta, 1)}</td>
      <td class="num hide-s">${comp("fundamental")}</td><td class="num hide-s">${comp("valuation")}</td>
      <td class="num px" data-px></td><td class="num" data-dp></td></tr>`;
  }).join("");
  $("#empty").hidden = rows.length > 0;
  if (!rows.length && S.scores) $("#empty").textContent = "조건에 맞는 종목이 없습니다.";
  $("#pageinfo").textContent = `${S.page + 1} / ${pages} · ${all.length}개 종목`;
  $("#prev").disabled = S.page === 0; $("#next").disabled = S.page >= pages - 1;
  if (S.scores) {
    $("#stamp").textContent = `점수 계산 ${kst(S.scores.generated_at)} (한국시간) · ${S.scores.universe}`;
    const active = Object.values(S.scores.config.components).filter((c) => c.active).map((c) => c.label);
    const off = Object.values(S.scores.config.components).filter((c) => !c.active).map((c) => c.label);
    $("#scopeNote").textContent = `현재 점수는 ${active.join("·")}만 반영합니다 (섹터 내 백분위). ${off.join("·")}${off.length ? "은(는) 아직 구현 전이라 제외하고 가중치를 재분배했습니다." : ""}`;
  }
  S.visible = rows.map((s) => s.symbol);
  S.visible.forEach(paintQuote);
  syncLive();
}

function paintQuote(sym, flash) {
  const q = S.quotes.get(sym);
  const tr = $(`#rank tr[data-sym="${CSS.escape(sym)}"]`);
  if (tr) {
    const px = $("[data-px]", tr), dp = $("[data-dp]", tr);
    px.textContent = q ? fmt(q.c) : "—";
    px.classList.toggle("stale", !q?.live);
    px.title = q ? (q.live ? `실시간 · ${kst(q.t)}` : `수집 시점 가격 · ${kst(q.t)}`) : "";
    dp.textContent = q && q.dp != null ? sign(q.dp, 2, "%") : "—";
    dp.className = "num " + cls(q?.dp);
    if (flash) {
      px.classList.remove("fu", "fd"); void px.offsetWidth;
      px.classList.add(flash > 0 ? "fu" : "fd");
      setTimeout(() => px.classList.remove("fu", "fd"), 60);
    }
  }
  if (S.detail === sym) paintDetailQuote();
}

// ---------- 실시간 시세 ----------
function setLive(text, on) { const el = $("#live"); el.textContent = text; el.classList.toggle("on", !!on); el.classList.toggle("off", !on); }

function syncLive() {
  $("#keyBanner").hidden = !!S.key;
  if (!S.key) { closeWs(); setLive("실시간 꺼짐", false); return; }
  const want = new Set(S.visible);
  if (S.detail) want.add(S.detail);
  if (!S.ws || S.ws.readyState > 1) openWs();
  if (S.wsOpen) {
    for (const s of S.subs) if (!want.has(s)) { S.ws.send(JSON.stringify({ type: "unsubscribe", symbol: s })); S.subs.delete(s); }
    for (const s of want) if (!S.subs.has(s)) { S.ws.send(JSON.stringify({ type: "subscribe", symbol: s })); S.subs.add(s); }
  }
  refreshQuotes([...want]);
}

function openWs() {
  closeWs();
  let ws;
  try { ws = new WebSocket(`wss://ws.finnhub.io?token=${encodeURIComponent(S.key)}`); } catch { return; }
  S.ws = ws;
  setLive("연결 중…", false);
  ws.onopen = () => { if (S.ws !== ws) return; S.wsOpen = true; S.subs.clear(); setLive("실시간", true); syncLive(); };
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type !== "trade" || !Array.isArray(msg.data)) return;
    const last = new Map();
    for (const t of msg.data) last.set(t.s, t);          // 묶음의 마지막 체결만 반영
    for (const [sym, t] of last) {
      const old = S.quotes.get(sym) || {};
      const pc = old.pc;
      const q = { ...old, c: t.p, t: Math.floor(t.t / 1000), live: true };
      if (pc) { q.d = t.p - pc; q.dp = (t.p - pc) / pc * 100; }
      S.quotes.set(sym, q);
      paintQuote(sym, old.c == null || t.p === old.c ? 0 : t.p > old.c ? 1 : -1);
    }
  };
  ws.onclose = () => {
    if (S.ws !== ws) return;
    S.wsOpen = false; S.subs.clear(); S.ws = null;
    if (S.key) { setLive("재연결 대기", false); setTimeout(() => { if (S.key && !S.ws) syncLive(); }, 5000); }
  };
  ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
}
function closeWs() { const ws = S.ws; S.ws = null; S.wsOpen = false; S.subs.clear(); if (ws) { try { ws.close(); } catch { /* noop */ } } }

// REST 시세: 화면에 보이는 종목만, 약 1.2초 간격(분당 50회 이하)으로 차례로 받는다
async function refreshQuotes(symbols) {
  const run = ++S.quoteRun;
  for (const sym of symbols) {
    if (run !== S.quoteRun || !S.key) return;
    const cur = S.quotes.get(sym);
    if (cur?.live && Date.now() / 1000 - (cur.fetched || 0) < 90) continue;
    try {
      const q = await fh("/quote", { symbol: sym });
      if (q && q.c) {
        const old = S.quotes.get(sym);
        S.quotes.set(sym, { c: q.c, pc: q.pc, d: q.d, dp: q.dp, t: q.t, live: true, fetched: Date.now() / 1000 });
        paintQuote(sym, old && old.c !== q.c ? (q.c > old.c ? 1 : -1) : 0);
      }
      $("#keyStatus").textContent = "";
    } catch (e) {
      if (e.message === "401" || e.message === "403") { setLive("키 오류", false); $("#keyStatus").textContent = "Finnhub 가 이 키를 거부했습니다. 키를 확인하세요."; return; }
      if (e.message === "429") await sleep(20000);
    }
    await sleep(1200);
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- 뉴스 ----------
function newsItemHTML(n, fresh) {
  const tks = (n.tickers || []).filter((t) => S.bySymbol.has(t)).map((t) => `<button class="tk" data-sym="${esc(t)}">${esc(t)}</button>`).join(" ");
  const also = n.also?.length ? ` 외 ${n.also.length}곳` : "";
  return `<li class="${fresh ? "fresh" : ""}"><a class="title" href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>
    ${n.summary ? `<p class="sum">${esc(n.summary)}</p>` : ""}
    <div class="meta"><span>${esc(n.source)}${also}</span><span title="${esc(kst(n.published))}">${ago(n.published)}</span>${tks}</div></li>`;
}

function renderNews() {
  const all = mergedNews();
  const fresh = new Set();
  for (const n of all) if (!S.seenNews.has(n.id)) { if (!S.firstNewsLoad) fresh.add(n.id); S.seenNews.add(n.id); }
  if (!S.firstNewsLoad && fresh.size && S.tab !== "news") S.unseen += fresh.size;
  S.firstNewsLoad = false;
  const badge = $("#newsBadge"); badge.hidden = !S.unseen; badge.textContent = S.unseen;

  const sel = $("#newsSource"), cur = sel.value;
  const sources = [...new Set(all.map((n) => n.source))].sort();
  sel.innerHTML = `<option value="">전체 언론사</option>` + sources.map((x) => `<option>${esc(x)}</option>`).join("");
  sel.value = sources.includes(cur) ? cur : "";
  const tagged = $("#newsTagged").checked;
  const list = all.filter((n) => (!sel.value || n.source === sel.value) && (!tagged || (n.tickers || []).length)).slice(0, 200);
  $("#newsList").innerHTML = list.length ? list.map((n) => newsItemHTML(n, fresh.has(n.id))).join("") : `<li class="muted">표시할 헤드라인이 없습니다.</li>`;
  const parts = [];
  if (S.newsMeta) parts.push(`수집본 ${kst(S.newsMeta.generated_at)}`);
  if (S.key && S.liveNewsAt) parts.push(`직접 확인 ${kst(S.liveNewsAt)}`);
  $("#newsStamp").textContent = parts.join(" · ") + (parts.length ? " (한국시간)" : "");
  if (S.detail) renderDetailNews();
}

// ---------- 종목 상세 ----------
function openDetail(sym, keep) {
  const s = S.bySymbol.get(sym);
  if (!s) return;
  S.detail = sym;
  const cfg = S.scores.config.components;
  const delta = s.score != null && s.prev_score != null ? s.score - s.prev_score : null;
  const sections = Object.entries(cfg).map(([id, c]) => {
    const comp = s.components[id];
    const head = `<h3><span>${esc(c.label)} <span class="muted">· 가중치 ${Math.round(c.weight * 100)}%</span></span><span>${comp ? fmt(comp.score, 0) : "—"}</span></h3>`;
    if (!c.active) return `<div class="d-sec">${head}<div class="pending">아직 구현 전 (다음 단계) — 점수에서 제외</div></div>`;
    if (!comp) return `<div class="d-sec">${head}<div class="pending">데이터 없음 — 점수에서 제외하고 가중치 재분배</div></div>`;
    const rows = Object.entries(c.metrics).map(([m, spec]) => {
      const v = comp.metrics[m];
      if (!v) return `<div class="metric muted"><span>${esc(spec.label)}</span><span class="num">—</span><span class="track"></span><span class="num">—</span></div>`;
      return `<div class="metric"><span>${esc(spec.label)}</span><span class="num">${fmt(v.value, spec.unit === "x" ? 1 : 1)}${esc(spec.unit)}</span>
        <span class="track" title="섹터 내 백분위 (높을수록 유리)"><i style="width:${v.pct}%"></i></span><span class="num">${fmt(v.pct, 0)}</span></div>`;
    }).join("");
    return `<div class="d-sec">${head}${rows}</div>`;
  }).join("");
  $("#detail").innerHTML = `
    <div class="d-head"><div><div class="big">${esc(s.symbol)}</div><div class="muted">${esc(s.name)} · ${esc(s.sector)}</div></div>
      <div class="num"><div class="d-score">${s.score == null ? "—" : fmt(s.score, 1)}</div><div class="small ${cls(delta)}">${delta == null ? "전일 점수 없음" : "전일 대비 " + sign(delta, 1)}</div></div></div>
    <div id="dq" class="d-sec"></div>
    ${S.scores.sample ? `<div class="pending">샘플 데이터 — 실제 수치가 아닙니다.</div>` : ""}
    ${sections}
    <div class="d-sec"><h3><span>최근 뉴스</span></h3><ul class="d-news" id="dnews"><li class="muted">불러오는 중…</li></ul></div>
    <p class="small muted">점수 계산 ${kst(S.scores.generated_at)} · 막대는 같은 섹터 안에서의 백분위입니다. 투자 권유가 아닙니다.</p>`;
  $("#drawer").hidden = false; $("#scrim").hidden = false;
  paintDetailQuote(); renderDetailNews();
  if (!keep) { S.companyNews = null; loadCompanyNews(sym); syncLive(); }
}

function paintDetailQuote() {
  const q = S.quotes.get(S.detail), el = $("#dq");
  if (!el) return;
  el.innerHTML = q ? `<span style="font-size:20px;font-weight:700">$${fmt(q.c)}</span> <span class="${cls(q.dp)}">${sign(q.d)} (${sign(q.dp, 2, "%")})</span>
    <div class="small muted">${q.live ? "실시간" : "수집 시점 가격"} · ${kst(q.t)} (한국시간)</div>` : `<span class="muted">가격 정보 없음</span>`;
}

async function loadCompanyNews(sym) {
  if (!S.key) return;
  try {
    const day = (d) => d.toISOString().slice(0, 10);
    const raw = await fh("/company-news", { symbol: sym, from: day(new Date(Date.now() - 7 * 864e5)), to: day(new Date()) });
    if (S.detail !== sym) return;
    S.companyNews = (raw || []).filter((n) => n.headline && n.url).slice(0, 15).map((n) => ({
      id: "fc" + n.id, title: n.headline, url: n.url, source: n.source || "Finnhub", published: new Date(n.datetime * 1000).toISOString(),
    }));
    renderDetailNews();
  } catch { /* 수집본만 표시 */ }
}

function renderDetailNews() {
  const el = $("#dnews");
  if (!el) return;
  const tagged = mergedNews().filter((n) => (n.tickers || []).includes(S.detail));
  const seen = new Set(), list = [];
  for (const n of [...tagged, ...(S.companyNews || [])]) { if (!seen.has(n.title)) { seen.add(n.title); list.push(n); } }
  list.sort((a, b) => (a.published < b.published ? 1 : -1));
  el.innerHTML = list.length ? list.slice(0, 15).map((n) => `<li><a href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>
    <div class="small muted">${esc(n.source)} · ${ago(n.published)}</div></li>`).join("")
    : `<li class="muted">최근 7일 안에 이 종목으로 인식된 헤드라인이 없습니다.${S.key ? "" : " (키를 넣으면 종목별 뉴스를 직접 조회합니다)"}</li>`;
}
function closeDetail() { S.detail = null; $("#drawer").hidden = true; $("#scrim").hidden = true; syncLive(); }

// ---------- 설정 ----------
function renderWeights() {
  if (!S.scores) return;
  const c = S.scores.config.components;
  $("#weights").innerHTML = `<ul class="plain">` + Object.values(c).map((x) =>
    `<li><b>${esc(x.label)}</b> ${Math.round(x.weight * 100)}% ${x.active ? "— " + Object.values(x.metrics).map((m) => esc(m.label)).join(", ") : `<span class="muted">— 아직 구현 전 (제외)</span>`}</li>`).join("") +
    `</ul><p class="small muted">가중치는 출발점으로 잡은 임의값이며 config/weights.yaml 에서 바꿉니다. 시장 국면 보정 계수는 현재 ${S.scores.config.regime.current ?? 1} (고정).</p>`;
}

function setTab(tab) {
  S.tab = tab;
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  for (const t of ["rank", "news", "settings"]) $(`#tab-${t}`).hidden = t !== tab;
  if (tab === "news") { S.unseen = 0; $("#newsBadge").hidden = true; }
}

// ---------- 이벤트 ----------
$$(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-goto]");
  if (go) { e.preventDefault(); setTab(go.dataset.goto); return; }
  const tk = e.target.closest(".tk");
  if (tk) { openDetail(tk.dataset.sym); return; }
  const tr = e.target.closest("#rank tbody tr");
  if (tr) openDetail(tr.dataset.sym);
});
$("#q").addEventListener("input", (e) => { S.q = e.target.value; S.page = 0; renderRank(); });
$("#sector").addEventListener("change", (e) => { S.sector = e.target.value; S.page = 0; renderRank(); });
$("#prev").addEventListener("click", () => { S.page--; renderRank(); });
$("#next").addEventListener("click", () => { S.page++; renderRank(); });
$("#newsSource").addEventListener("change", renderNews);
$("#newsTagged").addEventListener("change", renderNews);
$("#closeDrawer").addEventListener("click", closeDetail);
$("#scrim").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.detail) closeDetail(); });
$("#saveKey").addEventListener("click", () => {
  const v = $("#key").value.trim();
  if (!v) return;
  S.key = v;
  try { localStorage.setItem(KEY_NAME, v); } catch { /* 이번 세션에만 유지 */ }
  $("#key").value = ""; $("#keyStatus").textContent = "저장했습니다. 실시간 시세에 연결합니다.";
  closeWs(); syncLive(); loadNews();
});
$("#clearKey").addEventListener("click", () => {
  S.key = ""; S.liveNews = [];
  try { localStorage.removeItem(KEY_NAME); } catch { /* noop */ }
  $("#keyStatus").textContent = "키를 지웠습니다."; syncLive(); renderNews();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadNews(); loadScores(); syncLive(); } });

// ---------- 시작 ----------
if (S.key) $("#keyStatus").textContent = "이 브라우저에 키가 저장되어 있습니다.";
renderMarket(); setInterval(renderMarket, 30000);
loadScores().then(loadNews);
setInterval(() => { if (!document.hidden) loadNews(); }, 60000);
setInterval(() => { if (!document.hidden) loadScores(); }, 300000);
setInterval(() => { if (!document.hidden && S.key) syncLive(); }, 120000);
