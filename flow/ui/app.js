/* ============================================================
   Bomboclat Flow — single-page UI controller (vanilla JS)
   ============================================================ */
(function () {
  "use strict";

  /* ---------- inline SVG icons ---------- */
  const ICON = {
    search:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>',
    copy:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>',
    check:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4 10-10"/></svg>',
    trash:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5v2"/><path d="M6 7l1 12.5A1.5 1.5 0 0 0 8.5 21h7A1.5 1.5 0 0 0 17 19.5L18 7"/></svg>',
    pinOutline:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6Z"/><path d="M12 14v7"/></svg>',
    pinFilled:
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="currentColor" stroke-width="1" stroke-linejoin="round"><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6Z"/><path d="M12 14v7" stroke-width="1.6" fill="none"/></svg>',
    spark:
      '<svg class="spark" viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><path d="M12 2.5l1.6 5.1a3 3 0 0 0 1.9 1.9L20.6 11l-5.1 1.6a3 3 0 0 0-1.9 1.9L12 19.6l-1.6-5.1a3 3 0 0 0-1.9-1.9L3.4 11l5.1-1.6a3 3 0 0 0 1.9-1.9L12 2.5Z"/><path d="M19 3.5l.5 1.6.5.5 1.6.5-1.6.5-.5.5-.5 1.6-.5-1.6-.5-.5-1.6-.5 1.6-.5.5-.5Z"/></svg>',
    trendUp:
      '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17 11 10l3.5 3.5L20 8"/><path d="M15 8h5v5"/></svg>',
    mic:
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0"/><path d="M12 17v4"/></svg>',
  };

  /* ---------- bridge ---------- */
  const HAS_NATIVE =
    typeof window.webkit !== "undefined" &&
    window.webkit.messageHandlers &&
    window.webkit.messageHandlers.bridge;

  let msgCounter = 0;
  const pending = new Map();

  window.BF = {
    onMessage(resp) {
      if (!resp || typeof resp.id === "undefined") return;
      const p = pending.get(resp.id);
      if (!p) return;
      pending.delete(resp.id);
      if (resp.ok) p.resolve(resp.data);
      else p.reject(new Error(String(resp.data)));
    },
  };

  function sendMessage(action, payload) {
    if (!HAS_NATIVE) return mockDispatch(action, payload || {});
    return new Promise((resolve, reject) => {
      const id = ++msgCounter;
      pending.set(id, { resolve, reject });
      const msg = { id: id, action: action, payload: payload || {} };
      try {
        window.webkit.messageHandlers.bridge.postMessage(JSON.stringify(msg));
      } catch (e) {
        pending.delete(id);
        reject(e);
      }
    });
  }

  /* ============================================================
     MOCK DATA (browser preview / standalone)
     ============================================================ */
  const MOCK = buildMock();

  function iso(daysAgo, h, m) {
    const d = new Date();
    d.setDate(d.getDate() - daysAgo);
    d.setHours(h, m, 0, 0);
    // store as UTC ISO (contract: ts is UTC); toISOString gives Z time
    return d.toISOString().replace(/\.\d{3}Z$/, "");
  }

  function buildMock() {
    let idc = 1000;
    const mk = (daysAgo, h, m, text, app, pinned) => ({
      id: idc++,
      ts: iso(daysAgo, h, m),
      text: text,
      pinned: !!pinned,
      app: app,
    });

    const entries = [
      mk(0, 9, 12, "Let's ship the transcription pipeline refactor before the demo. I split the MLX stream handling onto its own thread so the UI never blocks while Parakeet decodes.", "Slack", true),
      mk(0, 8, 41, "Reminder to myself: add a warning banner when Accessibility permission is missing, otherwise the hotkey silently fails and users think the app is broken.", "Notes"),
      mk(0, 8, 3, "Quick note, the cream palette reads much warmer than the stock Wispr grey. Keep the petrol accent, it grounds everything.", "Obsidian"),
      mk(1, 17, 55, "Draft reply to Mara: thanks for the thorough review, I've folded in the streaming fix and pushed a new build. Let me know if the latency feels better on your M1.", "Mail", true),
      mk(1, 14, 22, "Grocery run later — oat milk, sourdough, the good olive oil, and those little tomatoes on the vine.", "Notes"),
      mk(1, 11, 9, "The dictation dictionary should learn proper nouns automatically after it sees them twice. Manual entry is the fallback, not the default.", "Linear"),
      mk(2, 16, 40, "Meeting recap: we agreed to keep everything local, no cloud round trips. Privacy is the whole point, so the store stays in Application Support and never leaves the machine.", "Slack"),
      mk(2, 10, 15, "Bomboclat means chaos in a good way. The name should feel a little playful against a very calm, precise interface.", "Notes"),
      mk(4, 13, 30, "Benchmarked words per minute across a week of real use — hovering around a hundred and forty, which tracks with comfortable speaking pace.", "Numbers"),
      mk(5, 9, 5, "Fix the heatmap so it always renders a full twenty one week grid even when the earliest data starts mid week. Pad the leading days.", "Linear"),
      mk(8, 15, 48, "Longer thought about focus: dictation removes the friction between having an idea and getting it down. The transcription just needs to disappear.", "Obsidian"),
    ];

    // 90 days of daily word counts (date -> words)
    const daily = [];
    const today = new Date();
    for (let i = 89; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      const dow = d.getDay();
      // weekdays busier; some zero days to create realistic streaks/gaps
      let base = dow === 0 || dow === 6 ? 60 : 380;
      const noise = Math.floor(Math.abs(Math.sin(i * 1.3) * 340));
      let w = base + noise;
      // scatter a few off days
      if ((i * 7) % 11 === 0 && i > 3) w = 0;
      const ds = d.toISOString().slice(0, 10);
      if (w > 0) daily.push({ date: ds, words: w });
    }
    const totalWords = 23540;

    const insights = {
      wpm: 142,
      totalWords: totalWords,
      monthTrendPct: 18,
      fixesTotal: 1284,
      wordsCorrected: 946,
      dictFixes: 338,
      apps: [
        { name: "Slack", count: 214, pct: 34 },
        { name: "Mail", count: 158, pct: 25 },
        { name: "Notes", count: 120, pct: 19 },
        { name: "Obsidian", count: 82, pct: 13 },
        { name: "Linear", count: 57, pct: 9 },
      ],
      daily: daily,
      streak: { current: 6, longest: 21 },
    };

    const dictionary = {
      words: [
        { word: "Bomboclat", auto: false, added_ts: iso(9, 10, 0) },
        { word: "MLX", auto: true, added_ts: iso(6, 10, 0) },
        { word: "Parakeet", auto: true, added_ts: iso(6, 11, 0) },
        { word: "Haberstroh", auto: false, added_ts: iso(12, 9, 0) },
        { word: "PyObjC", auto: true, added_ts: iso(4, 14, 0) },
        { word: "petrol", auto: false, added_ts: iso(3, 8, 0) },
        { word: "Wispr", auto: true, added_ts: iso(8, 8, 0) },
        { word: "WKWebView", auto: true, added_ts: iso(2, 16, 0) },
      ],
    };

    return { entries, insights, dictionary };
  }

  function mockDispatch(action, payload) {
    return new Promise((resolve) => {
      setTimeout(() => {
        switch (action) {
          case "getHome": {
            const ins = MOCK.insights;
            resolve({
              userName: "Jonathan",
              stats: {
                totalWords: ins.totalWords,
                wpm: ins.wpm,
                streak: ins.streak.current,
              },
              entries: MOCK.entries.slice(),
            });
            break;
          }
          case "getInsights":
            resolve(JSON.parse(JSON.stringify(MOCK.insights)));
            break;
          case "getDictionary":
            resolve({ words: MOCK.dictionary.words.slice() });
            break;
          case "setPinned": {
            const e = MOCK.entries.find((x) => x.id === payload.id);
            if (e) e.pinned = !!payload.pinned;
            resolve(null);
            break;
          }
          case "deleteEntry": {
            const i = MOCK.entries.findIndex((x) => x.id === payload.id);
            if (i >= 0) MOCK.entries.splice(i, 1);
            resolve(null);
            break;
          }
          case "copyText":
            resolve(null);
            break;
          case "addWord": {
            const w = payload.word.trim();
            if (w && !MOCK.dictionary.words.some((x) => x.word.toLowerCase() === w.toLowerCase())) {
              MOCK.dictionary.words.push({ word: w, auto: false, added_ts: new Date().toISOString() });
            }
            resolve(null);
            break;
          }
          case "deleteWord": {
            MOCK.dictionary.words = MOCK.dictionary.words.filter(
              (x) => x.word.toLowerCase() !== payload.word.toLowerCase()
            );
            resolve(null);
            break;
          }
          default:
            resolve(null);
        }
      }, HAS_NATIVE ? 0 : 40);
    });
  }

  /* ============================================================
     UTILITIES
     ============================================================ */
  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }
  function parseTs(ts) {
    // ts is ISO-8601 UTC (may lack the Z). Ensure UTC interpretation.
    let s = String(ts);
    if (!/[zZ]|[+\-]\d{2}:\d{2}$/.test(s)) s += "Z";
    const d = new Date(s);
    return isNaN(d.getTime()) ? new Date() : d;
  }
  function localTime(ts) {
    const d = parseTs(ts);
    let h = d.getHours();
    const m = d.getMinutes();
    const ap = h >= 12 ? "pm" : "am";
    h = h % 12;
    if (h === 0) h = 12;
    return h + ":" + String(m).padStart(2, "0") + " " + ap;
  }
  function localDateKey(ts) {
    const d = parseTs(ts);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  const MONTHS = ["JANUARY","FEBRUARY","MARCH","APRIL","MAY","JUNE","JULY","AUGUST","SEPTEMBER","OCTOBER","NOVEMBER","DECEMBER"];
  const MONTHS_SHORT = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function dayHeaderLabel(dateKey) {
    const now = new Date();
    const todayKey = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0");
    const y = new Date(now); y.setDate(y.getDate() - 1);
    const yKey = y.getFullYear() + "-" + String(y.getMonth() + 1).padStart(2, "0") + "-" + String(y.getDate()).padStart(2, "0");
    if (dateKey === todayKey) return "TODAY";
    if (dateKey === yKey) return "YESTERDAY";
    const parts = dateKey.split("-").map(Number);
    return MONTHS[parts[1] - 1] + " " + parts[2] + ", " + parts[0];
  }
  function fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 1000) {
      const k = n / 1000;
      return (k >= 100 ? Math.round(k) : Math.round(k * 10) / 10) + "K";
    }
    return String(n);
  }

  /* ============================================================
     VIEW: HOME
     ============================================================ */
  const homeState = { data: null, query: "", expanded: new Set() };

  async function loadHome() {
    homeState.data = await sendMessage("getHome");
    renderHome();
  }

  function renderHome() {
    const root = document.getElementById("view-home");
    const d = homeState.data || { userName: "there", stats: {}, entries: [] };
    const stats = d.stats || {};

    root.innerHTML = "";
    root.appendChild(
      el(
        `<div class="home-head">
          <div>
            <h1>Welcome back, ${esc(d.userName || "there")}</h1>
            <div class="sub">Here's everything you've dictated, kept private on this Mac.</div>
          </div>
          <div class="card stat-mini">
            <div class="row"><span class="num serif">${fmtNum(stats.totalWords)}</span><span class="lab">total words</span></div>
            <div class="row"><span class="num serif">${Number(stats.wpm) || 0}</span><span class="lab">wpm</span></div>
            <div class="row"><span class="num serif">${Number(stats.streak) || 0}</span><span class="lab">day streak</span></div>
          </div>
        </div>`
      )
    );

    const tools = el(
      `<div class="home-tools">
        <div class="search">
          ${ICON.search}
          <input class="input" type="text" id="home-search" placeholder="Search your dictations" value="${esc(homeState.query)}" autocomplete="off" spellcheck="false" />
        </div>
      </div>`
    );
    root.appendChild(tools);

    const listWrap = el(`<div id="home-list"></div>`);
    root.appendChild(listWrap);
    renderHomeList();

    const searchInput = document.getElementById("home-search");
    searchInput.addEventListener("input", (e) => {
      homeState.query = e.target.value;
      renderHomeList();
    });
  }

  function renderHomeList() {
    const wrap = document.getElementById("home-list");
    if (!wrap) return;
    const d = homeState.data || { entries: [] };
    const q = homeState.query.trim().toLowerCase();
    let entries = d.entries || [];
    if (q) entries = entries.filter((e) => (e.text || "").toLowerCase().includes(q));

    wrap.innerHTML = "";

    if (!entries.length) {
      wrap.appendChild(
        el(
          `<div class="empty">
            <div class="empty-mark">${ICON.mic}</div>
            <div class="empty-title">${q ? "No matches" : "No dictations yet"}</div>
            <p>${q ? "Try a different search term." : "Hold your hotkey and speak — your words will show up here."}</p>
          </div>`
        )
      );
      return;
    }

    // sort: newest first (entries arrive newest-first already), then group by local date
    const sorted = entries.slice().sort((a, b) => parseTs(b.ts) - parseTs(a.ts));
    const groups = [];
    const index = new Map();
    for (const e of sorted) {
      const key = localDateKey(e.ts);
      if (!index.has(key)) {
        index.set(key, groups.length);
        groups.push({ key: key, items: [] });
      }
      groups[index.get(key)].items.push(e);
    }

    for (const g of groups) {
      const group = el(`<div class="day-group"></div>`);
      group.appendChild(el(`<div class="day-head label">${dayHeaderLabel(g.key)}</div>`));
      for (const e of g.items) group.appendChild(entryCard(e));
      wrap.appendChild(group);
    }
  }

  function entryCard(e) {
    const expanded = homeState.expanded.has(e.id);
    const card = el(
      `<div class="card entry${expanded ? " is-expanded" : ""}" data-id="${e.id}">
        <div class="etime">${localTime(e.ts)}</div>
        <div class="ebody">
          <div class="etext">${esc(e.text)}</div>
          ${e.app ? `<div class="emeta"><span class="eapp">${esc(e.app)}</span></div>` : ""}
        </div>
        ${e.pinned ? `<span class="pin-static">${ICON.pinFilled}</span>` : ""}
        <div class="eactions">
          <button class="icon-btn act-copy" title="Copy" aria-label="Copy">${ICON.copy}</button>
          <button class="icon-btn act-pin${e.pinned ? " is-on" : ""}" title="${e.pinned ? "Unpin" : "Pin"}" aria-label="Pin">${e.pinned ? ICON.pinFilled : ICON.pinOutline}</button>
          <button class="icon-btn is-danger act-del" title="Delete" aria-label="Delete">${ICON.trash}</button>
        </div>
      </div>`
    );

    card.querySelector(".etext").addEventListener("click", () => {
      if (homeState.expanded.has(e.id)) homeState.expanded.delete(e.id);
      else homeState.expanded.add(e.id);
      card.classList.toggle("is-expanded");
    });

    card.querySelector(".act-copy").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      await sendMessage("copyText", { id: e.id });
      if (!HAS_NATIVE) {
        try { await navigator.clipboard.writeText(e.text); } catch (_) {}
      }
      btn.innerHTML = ICON.check;
      btn.classList.add("is-on");
      setTimeout(() => { btn.innerHTML = ICON.copy; btn.classList.remove("is-on"); }, 1100);
    });

    card.querySelector(".act-pin").addEventListener("click", async () => {
      await sendMessage("setPinned", { id: e.id, pinned: !e.pinned });
      await loadHome();
    });

    card.querySelector(".act-del").addEventListener("click", async () => {
      await sendMessage("deleteEntry", { id: e.id });
      homeState.expanded.delete(e.id);
      await loadHome();
    });

    return card;
  }

  /* ============================================================
     VIEW: INSIGHTS
     ============================================================ */
  async function loadInsights() {
    const d = await sendMessage("getInsights");
    renderInsights(d || {});
  }

  function renderInsights(d) {
    const root = document.getElementById("view-insights");
    const wpm = Number(d.wpm) || 0;
    const streak = d.streak || { current: 0, longest: 0 };
    const apps = Array.isArray(d.apps) ? d.apps : [];
    const daily = Array.isArray(d.daily) ? d.daily : [];
    const trend = Number(d.monthTrendPct) || 0;

    root.innerHTML = "";
    root.appendChild(el(`<h1>Insights</h1>`));

    // ----- row of 3 -----
    const row3 = el(`<div class="grid-3"></div>`);

    // (1) WPM + gauge
    row3.appendChild(
      el(
        `<div class="card icard">
          <div class="label">Speaking pace</div>
          <div class="big-stat">${wpm}</div>
          <div class="gauge-wrap">${gaugeSVG(wpm)}</div>
          <div class="label" style="margin-top:12px;text-align:center">Words per minute</div>
        </div>`
      )
    );

    // (2) fixes
    row3.appendChild(
      el(
        `<div class="card icard">
          <div class="label">Fixes made by Flow</div>
          <div class="big-stat fixes-total">${fmtNum(d.fixesTotal || 0)}</div>
          <div class="fixes-sub">
            <div class="fx"><span>Words corrected</span><b>${fmtNum(d.wordsCorrected || 0)}</b></div>
            <div class="fx"><span>Dictionary fixes</span><b>${fmtNum(d.dictFixes || 0)}</b></div>
          </div>
        </div>`
      )
    );

    // (3) total words
    row3.appendChild(
      el(
        `<div class="card icard">
          <div class="label">Total words dictated</div>
          <div class="big-stat">${fmtNum(d.totalWords || 0)}</div>
          ${trend !== 0 ? `<span class="trend-pill">${ICON.trendUp}${trend > 0 ? "+" : ""}${trend}% this month</span>` : ""}
        </div>`
      )
    );
    root.appendChild(row3);

    // ----- row of 2 -----
    const row2 = el(`<div class="grid-2"></div>`);

    // (1) desktop usage
    const usage = el(
      `<div class="card icard">
        <div class="usage-head">
          <h3>Desktop usage</h3>
          <div class="label">Total apps used | ${apps.length}</div>
        </div>
        <div class="bars"></div>
      </div>`
    );
    const bars = usage.querySelector(".bars");
    if (!apps.length) {
      bars.appendChild(el(`<div class="empty" style="padding:24px 0"><p>No app usage recorded yet.</p></div>`));
    } else {
      const maxPct = Math.max.apply(null, apps.map((a) => a.pct || 0)) || 1;
      for (const a of apps) {
        const pct = Number(a.pct) || 0;
        const w = Math.max(8, Math.round((pct / maxPct) * 100));
        bars.appendChild(
          el(
            `<div class="bar-row">
              <div class="bar-track"><div class="bar-fill" style="width:${w}%">${pct}%</div></div>
              <div class="bar-app">${esc(a.name)}</div>
            </div>`
          )
        );
      }
    }
    row2.appendChild(usage);

    // (2) streak heatmap
    row2.appendChild(
      el(
        `<div class="card icard">
          <div class="heat-head">
            <h3>${streak.current || 0} day streak</h3>
            <div class="label">Longest streak | ${streak.longest || 0} days</div>
          </div>
          ${heatmapHTML(daily)}
        </div>`
      )
    );
    root.appendChild(row2);

    // animate bar widths in after paint
    requestAnimationFrame(() => {
      root.querySelectorAll(".bar-fill").forEach((b) => {
        const w = b.style.width;
        b.style.width = "0%";
        requestAnimationFrame(() => (b.style.width = w));
      });
    });
  }

  function gaugeSVG(wpm) {
    // half-circle gauge, radius 70, semicircle length = PI*r
    const r = 70;
    const cx = 80, cy = 78;
    const len = Math.PI * r;
    const frac = Math.min(wpm, 160) / 160;
    const dash = frac * len;
    return (
      `<svg class="gauge" viewBox="0 0 160 88">
        <path class="gauge-track" fill="none" stroke-width="12" stroke-linecap="round"
          d="M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}"
          stroke-dasharray="${len} ${len}" stroke-dashoffset="0"/>
        <path class="gauge-fill" fill="none" stroke-width="12" stroke-linecap="round"
          d="M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}"
          stroke-dasharray="${dash} ${len}" stroke-dashoffset="0"/>
      </svg>`
    );
  }

  function heatmapHTML(daily) {
    // build a lookup date -> words
    const map = new Map();
    let maxW = 0;
    for (const row of daily) {
      map.set(row.date, row.words || 0);
      if ((row.words || 0) > maxW) maxW = row.words || 0;
    }

    const WEEKS = 21;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    // end at the Saturday of the current week so grid columns align by week
    const end = new Date(today);
    end.setDate(end.getDate() + (6 - end.getDay())); // upcoming Saturday
    // start: go back WEEKS*7-1 days, then back to Sunday
    const start = new Date(end);
    start.setDate(start.getDate() - (WEEKS * 7 - 1));
    // start already lands on a Sunday because end is Saturday

    function keyOf(d) {
      return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    }
    function bucket(w) {
      if (!w) return 0;
      const f = w / (maxW || 1);
      if (f > 0.66) return 4;
      if (f > 0.4) return 3;
      if (f > 0.15) return 2;
      return 1;
    }

    // build columns (weeks) each with 7 rows (Sun..Sat)
    let cols = "";
    let monthCells = "";
    let lastMonth = -1;
    const cur = new Date(start);
    for (let wk = 0; wk < WEEKS; wk++) {
      // month label: show when the first day of the week (Sunday) is in a new month
      const colMonth = cur.getMonth();
      if (colMonth !== lastMonth) {
        monthCells += `<span>${MONTHS_SHORT[colMonth]}</span>`;
        lastMonth = colMonth;
      } else {
        monthCells += `<span></span>`;
      }
      let cells = "";
      for (let dow = 0; dow < 7; dow++) {
        const k = keyOf(cur);
        const future = cur > today;
        if (future) {
          cells += `<div class="heat-cell empty"></div>`;
        } else {
          const w = map.get(k) || 0;
          const b = bucket(w);
          const title = `${k}: ${w} words`;
          cells += `<div class="heat-cell l${b}" title="${title}"></div>`;
        }
        cur.setDate(cur.getDate() + 1);
      }
      cols += `<div class="heat-col">${cells}</div>`;
    }

    return (
      `<div class="heat-scroll">
        <div class="heat-grid-wrap">
          <div class="heat-months" style="grid-template-columns:repeat(${WEEKS},13px)">${monthCells}</div>
          <div class="heat-body">
            <div class="heat-daylabels"><span></span><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span></div>
            <div class="heat-cols">${cols}</div>
          </div>
        </div>
      </div>
      <div class="heat-legend">
        <span>Less</span>
        <div class="heat-cell l0"></div>
        <div class="heat-cell l1"></div>
        <div class="heat-cell l2"></div>
        <div class="heat-cell l3"></div>
        <div class="heat-cell l4"></div>
        <span>More</span>
      </div>`
    );
  }

  /* ============================================================
     VIEW: DICTIONARY
     ============================================================ */
  const dictState = { words: [], query: "", adding: false };

  async function loadDictionary() {
    const d = await sendMessage("getDictionary");
    dictState.words = (d && d.words) || [];
    renderDictionary();
  }

  function renderDictionary() {
    const root = document.getElementById("view-dictionary");
    root.innerHTML = "";

    root.appendChild(
      el(
        `<div class="dict-head">
          <h1>Dictionary</h1>
          <button class="btn btn-primary" id="dict-add-btn" type="button">+ Add new</button>
        </div>`
      )
    );

    root.appendChild(
      el(
        `<div class="hero">
          <h2>Bomboclat Flow spells the way you do.</h2>
          <p>Terms are learned automatically from your dictations ${ICON.spark} or added by hand.</p>
        </div>`
      )
    );

    const addrow = el(
      `<div class="addrow${dictState.adding ? "" : " hidden"}">
        <input class="input input-plain" id="dict-input" type="text" placeholder="Add a word or phrase, then press Enter" autocomplete="off" spellcheck="false" />
        <button class="btn btn-primary" id="dict-save" type="button">Save</button>
        <button class="btn btn-ghost" id="dict-cancel" type="button">Cancel</button>
      </div>`
    );
    root.appendChild(addrow);

    root.appendChild(
      el(
        `<div class="search home-tools">
          ${ICON.search}
          <input class="input" type="text" id="dict-search" placeholder="Search dictionary" value="${esc(dictState.query)}" autocomplete="off" spellcheck="false" />
        </div>`
      )
    );

    root.appendChild(el(`<div id="dict-list"></div>`));
    renderDictList();

    // wire controls
    document.getElementById("dict-add-btn").addEventListener("click", () => {
      dictState.adding = true;
      renderDictionary();
      const inp = document.getElementById("dict-input");
      if (inp) inp.focus();
    });
    document.getElementById("dict-search").addEventListener("input", (e) => {
      dictState.query = e.target.value;
      renderDictList();
    });

    if (dictState.adding) {
      const inp = document.getElementById("dict-input");
      const save = async () => {
        const w = inp.value.trim();
        if (!w) { dictState.adding = false; renderDictionary(); return; }
        await sendMessage("addWord", { word: w });
        dictState.adding = false;
        await loadDictionary();
      };
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); dictState.adding = false; renderDictionary(); }
      });
      document.getElementById("dict-save").addEventListener("click", save);
      document.getElementById("dict-cancel").addEventListener("click", () => {
        dictState.adding = false;
        renderDictionary();
      });
      inp.focus();
    }
  }

  function renderDictList() {
    const wrap = document.getElementById("dict-list");
    if (!wrap) return;
    const q = dictState.query.trim().toLowerCase();
    let words = dictState.words.slice();
    if (q) words = words.filter((w) => w.word.toLowerCase().includes(q));
    words.sort((a, b) => a.word.toLowerCase().localeCompare(b.word.toLowerCase()));

    wrap.innerHTML = "";

    if (!words.length) {
      wrap.appendChild(
        el(
          `<div class="empty">
            <div class="empty-title">${q ? "No matches" : "Your dictionary is empty"}</div>
            <p>${q ? "Try a different search." : "Add a word above, or let Flow learn terms as you dictate."}</p>
          </div>`
        )
      );
      return;
    }

    wrap.appendChild(el(`<div class="dict-count label">${dictState.words.length} words</div>`));
    const list = el(`<div class="card word-list"></div>`);
    for (const w of words) {
      const row = el(
        `<div class="word-row">
          <span class="w">${esc(w.word)}</span>
          ${w.auto ? ICON.spark : ""}
          <button class="icon-btn is-danger del" title="Remove" aria-label="Remove ${esc(w.word)}">${ICON.trash}</button>
        </div>`
      );
      row.querySelector(".del").addEventListener("click", async () => {
        await sendMessage("deleteWord", { word: w.word });
        await loadDictionary();
      });
      list.appendChild(row);
    }
    wrap.appendChild(list);
  }

  /* ============================================================
     NAV / ROUTING
     ============================================================ */
  const LOADERS = { home: loadHome, insights: loadInsights, dictionary: loadDictionary };

  function switchView(name) {
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.view === name)
    );
    document.querySelectorAll(".view").forEach((v) => {
      const on = v.id === "view-" + name;
      v.classList.toggle("is-active", on);
      v.setAttribute("aria-hidden", on ? "false" : "true");
    });
    const load = LOADERS[name];
    if (load) load().catch((err) => console.error("load " + name, err));
  }

  document.querySelectorAll(".nav-item").forEach((b) => {
    b.addEventListener("click", () => switchView(b.dataset.view));
  });

  // boot
  switchView("home");
})();
