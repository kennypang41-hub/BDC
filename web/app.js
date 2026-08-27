import { barChart, fmt, heatmap, hideTip, lineChart, showTip } from "./charts.js";

/* Data access. The static export and the API expose the same named views, so
   this is the only place that needs to know which one is serving us. */
const Data = {
  mode: "static",
  period: null,
  meta: null,
  cache: new Map(),

  async init() {
    // A standalone build inlines the whole bundle, so there is nothing to fetch.
    if (globalThis.__BDC_BUNDLE__) {
      this.mode = "embedded";
      this.meta = globalThis.__BDC_BUNDLE__.meta;
      return;
    }
    try {
      const response = await fetch("data/meta.json", { cache: "no-store" });
      if (response.ok) {
        this.mode = "static";
        this.meta = await response.json();
        return;
      }
    } catch (error) {
      /* fall through to the API */
    }
    this.mode = "api";
    this.meta = await (await fetch("/api/bundle/meta")).json();
  },

  url(name) {
    if (this.mode === "static") return `data/${name}.json`;
    const query = this.period ? `?period=${encodeURIComponent(this.period)}` : "";
    return `/api/bundle/${name}${query}`;
  },

  async get(name) {
    if (this.mode === "embedded") {
      const value = globalThis.__BDC_BUNDLE__[name];
      if (value === undefined) throw new Error(`${name}: not in the embedded bundle`);
      return value;
    }
    const key = `${name}@${this.period || "latest"}`;
    if (!this.cache.has(key)) {
      const response = await fetch(this.url(name));
      if (!response.ok) throw new Error(`${name}: ${response.status}`);
      this.cache.set(key, await response.json());
    }
    return this.cache.get(key);
  },

  async positions(ticker) {
    if (this.mode === "embedded") {
      return globalThis.__BDC_BUNDLE__.positions[ticker] || [];
    }
    return this.get(`positions/${ticker}`);
  },
};

/* ---------------------------------------------------------------- tables */

function table(target, rows, columns, options = {}) {
  const node = typeof target === "string" ? document.getElementById(target) : target;
  node.replaceChildren();
  if (!rows || !rows.length) {
    node.append(Object.assign(document.createElement("p"),
      { className: "empty", textContent: options.empty || "Nothing to show." }));
    return;
  }

  const state = { key: options.sort || null, dir: options.dir || "desc" };
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column.label;
    if (column.text) th.className = "text";
    th.title = column.title || column.label;
    th.addEventListener("click", () => {
      state.dir = state.key === column.key && state.dir === "desc" ? "asc" : "desc";
      state.key = column.key;
      render();
    });
    headRow.append(th);
  }
  head.append(headRow);

  const body = document.createElement("tbody");
  const element = document.createElement("table");
  element.append(head, body);
  node.append(element);

  function render() {
    const sorted = [...rows];
    if (state.key) {
      const sign = state.dir === "desc" ? -1 : 1;
      sorted.sort((a, b) => {
        const x = a[state.key];
        const y = b[state.key];
        if (x == null) return 1;
        if (y == null) return -1;
        return typeof x === "number" ? sign * (x - y) : sign * String(x).localeCompare(String(y));
      });
    }
    for (const th of headRow.children) th.removeAttribute("data-dir");
    const index = columns.findIndex((c) => c.key === state.key);
    if (index >= 0) headRow.children[index].setAttribute("data-dir", state.dir);

    body.replaceChildren();
    for (const row of sorted.slice(0, options.limit || 500)) {
      const tr = document.createElement("tr");
      if (options.onClick) {
        tr.style.cursor = "pointer";
        tr.addEventListener("click", () => options.onClick(row));
      }
      for (const column of columns) {
        const td = document.createElement("td");
        if (column.text) td.className = "text";
        else td.className = "num";
        const rendered = column.render ? column.render(row) : column.format
          ? column.format(row[column.key]) : row[column.key] ?? "–";
        if (rendered instanceof Node) td.append(rendered);
        else td.textContent = rendered;
        tr.append(td);
      }
      body.append(tr);
    }
  }

  render();
}

const ACRONYMS = new Set(["DDTL", "PIK", "CLO", "LLC", "NA"]);

/** "FIRST_LIEN_LAST_OUT" -> "First lien last out"; "DDTL" stays "DDTL". */
function humanize(value) {
  if (!value) return "–";
  const raw = String(value);
  if (ACRONYMS.has(raw)) return raw;
  const text = raw.replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function pill(text, tone) {
  const span = document.createElement("span");
  span.className = `pill ${tone}`;
  span.textContent = text;
  return span;
}

function markCell(value) {
  if (value == null) return "–";
  const span = document.createElement("span");
  span.textContent = value.toFixed(1);
  if (value < 80) return pill(`▼ ${value.toFixed(1)}`, "critical");
  if (value < 90) return pill(`▽ ${value.toFixed(1)}`, "warning");
  return span;
}

function rangeBar(min, max, weighted) {
  const wrap = document.createElement("span");
  wrap.className = "rangebar";
  const scale = (value) => Math.max(0, Math.min(100, value));
  const bar = document.createElement("i");
  bar.style.left = `${scale(min)}%`;
  bar.style.width = `${Math.max(2, scale(max) - scale(min))}%`;
  const tick = document.createElement("b");
  tick.style.left = `${scale(weighted)}%`;
  wrap.append(bar, tick);
  wrap.title = `${min.toFixed(1)} – ${max.toFixed(1)} (weighted ${weighted.toFixed(1)})`;
  return wrap;
}

function tile(label, value, note) {
  const node = document.createElement("div");
  node.className = "tile";
  node.innerHTML = `<div class="label"></div><div class="value"></div><div class="note"></div>`;
  node.querySelector(".label").textContent = label;
  node.querySelector(".value").textContent = value;
  node.querySelector(".note").textContent = note || "";
  return node;
}

/* ------------------------------------------------------------------ views */

const views = {
  async overview() {
    const [overview, trend, histogram, deteriorating] = await Promise.all([
      Data.get("overview"), Data.get("nonaccrual_trend"),
      Data.get("mark_histogram"), Data.get("deteriorating"),
    ]);

    const tiles = document.getElementById("overview-tiles");
    tiles.replaceChildren(
      tile("Portfolio mark", fmt.mark(overview.portfolio_mark),
        "fair value ÷ par, debt positions"),
      tile("Fair value", fmt.money(overview.fair_value), `${fmt.num(overview.positions)} positions`),
      tile("BDCs", fmt.num(overview.bdcs), `${fmt.num(overview.issuers)} borrowers`),
      tile("Marks extracted", fmt.num(overview.total_marks),
        `${fmt.num(overview.total_loans)} loans · ${overview.periods.length} quarters`),
      tile("On non-accrual", overview.nonaccrual_pct == null ? "not disclosed"
        : fmt.pct(overview.nonaccrual_pct, 2), "share of fair value"),
    );

    lineChart(document.getElementById("chart-na-trend"), trend, {
      x: "period_end", y: "nonaccrual_pct", format: (v) => fmt.pct(v, 2),
      label: "Non-accrual share",
      extra: (row) => `${fmt.money(row.nonaccrual_fv)} across ${fmt.num(row.nonaccrual_positions)} positions`,
    });
    table("table-na-trend", trend, [
      { key: "period_end", label: "Quarter", text: true, format: fmt.quarter },
      { key: "nonaccrual_pct", label: "Non-accrual %", format: (v) => fmt.pct(v, 2) },
      { key: "nonaccrual_fv", label: "Non-accrual FV", format: fmt.money },
      { key: "total_fv", label: "Total FV", format: fmt.money },
      { key: "nonaccrual_positions", label: "Positions", format: fmt.num },
    ], { sort: "period_end", dir: "asc" });

    barChart(document.getElementById("chart-histogram"), histogram, {
      x: "bin_start", y: "positions", format: (v) => fmt.num(Math.round(v)),
      xLabel: (row) => row.bin_start, label: "Positions",
      tip: (row) => `Mark <b>${row.bin_start.toFixed(1)}–${(row.bin_start + 2.5).toFixed(1)}</b><br>` +
        `${fmt.num(row.positions)} positions · ${fmt.money(row.fair_value)}`,
    });
    table("table-histogram", histogram, [
      { key: "bin_start", label: "Mark from", text: true, format: (v) => v.toFixed(1) },
      { key: "positions", label: "Positions", format: fmt.num },
      { key: "fair_value", label: "Fair value", format: fmt.money },
    ], { sort: "bin_start", dir: "asc" });

    table("table-deteriorating", deteriorating, [
      { key: "issuer_name", label: "Borrower", text: true },
      { key: "ticker", label: "BDC", text: true },
      { key: "investment_type", label: "Instrument", text: true, format: humanize },
      { key: "industry", label: "Industry", text: true },
      { key: "mark_prior", label: "Prior mark", format: fmt.mark },
      { key: "mark_now", label: "Mark", render: (row) => markCell(row.mark_now) },
      { key: "mark_change", label: "Change", format: (v) => v == null ? "–" : v.toFixed(1) },
      { key: "fair_value", label: "Fair value", format: fmt.money },
    ], { sort: "mark_change", dir: "asc", limit: 100, empty: "Needs two quarters of history." });
  },

  async bdcs() {
    const rows = await Data.get("bdcs");
    table("table-bdcs", rows, [
      { key: "ticker", label: "BDC", text: true },
      { key: "bdc_name", label: "Name", text: true },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "fv_change_pct", label: "QoQ FV", format: (v) => v == null ? "–" : fmt.pct(v) },
      { key: "portfolio_mark", label: "Mark", render: (row) => markCell(row.portfolio_mark) },
      { key: "fv_over_cost", label: "FV / cost", format: (v) => v == null ? "–" : fmt.pct(v) },
      { key: "nonaccrual_pct_fv", label: "Non-accrual",
        title: "Share of fair value on non-accrual; blank where the filing did not disclose it",
        format: (v) => v == null ? "n/d" : fmt.pct(v, 2) },
      { key: "pik_pct_fv", label: "PIK", format: (v) => fmt.pct(v, 1) },
      { key: "stressed_pct_fv", label: "Stressed", title: "Share of debt fair value marked below 90",
        format: (v) => fmt.pct(v, 1) },
      { key: "top10_pct_fv", label: "Top 10", title: "Share of fair value in the ten largest borrowers",
        format: (v) => fmt.pct(v, 1) },
      { key: "avg_coupon", label: "Coupon", format: (v) => fmt.pct(v, 2) },
      { key: "positions", label: "Positions", format: fmt.num },
      { key: "issuers", label: "Borrowers", format: fmt.num },
    ], {
      sort: "fair_value",
      onClick: (row) => {
        document.getElementById("ticker").value = row.ticker;
        select("positions");
      },
    });
  },

  async nonaccruals() {
    const [trend, positions, byIndustry, byBdc] = await Promise.all([
      Data.get("nonaccrual_trend"), Data.get("nonaccruals"),
      Data.get("nonaccrual_by_industry"), Data.get("nonaccrual_by_bdc"),
    ]);
    const latest = trend[trend.length - 1] || {};
    document.getElementById("na-tiles").replaceChildren(
      tile("Non-accrual fair value", fmt.money(latest.nonaccrual_fv), fmt.quarter(latest.period_end)),
      tile("Share of portfolio", latest.nonaccrual_pct == null ? "not disclosed"
        : fmt.pct(latest.nonaccrual_pct, 2), "of total fair value"),
      tile("Positions", fmt.num(latest.nonaccrual_positions),
        `${fmt.num(latest.coverage)} of ${fmt.num(latest.positions)} with disclosed status`),
    );

    const bucketColumns = [
      { key: "bucket", label: "Bucket", text: true },
      { key: "nonaccrual_pct", label: "Non-accrual %", format: (v) => fmt.pct(v, 2) },
      { key: "nonaccrual_fv", label: "Non-accrual FV", format: fmt.money },
      { key: "total_fv", label: "Total FV", format: fmt.money },
    ];
    table("table-na-industry", byIndustry, bucketColumns, { sort: "nonaccrual_fv" });
    table("table-na-bdc", byBdc, bucketColumns, { sort: "nonaccrual_fv" });

    table("table-na-positions", positions, [
      { key: "issuer_name", label: "Borrower", text: true },
      { key: "ticker", label: "BDC", text: true },
      { key: "investment_type", label: "Instrument", text: true, format: humanize },
      { key: "industry", label: "Industry", text: true },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "principal", label: "Par", format: fmt.money },
      { key: "mark", label: "Mark", render: (row) => markCell(row.mark) },
      { key: "quarters_nonaccrual", label: "Quarters NA", format: fmt.num },
      { key: "since", label: "Since", text: true, format: fmt.quarter },
      { key: "maturity_date", label: "Maturity", text: true },
    ], { sort: "fair_value", empty: "No non-accruals disclosed in this period." });
  },

  async marks() {
    const [sectors, markdowns] = await Promise.all([
      Data.get("sector_marks"), Data.get("markdowns"),
    ]);
    heatmap(document.getElementById("chart-sectors"), sectors, {
      row: "industry", col: "period_end", value: "weighted_mark",
      label: "Weighted average mark by sector and quarter",
    });
    table("table-sectors", sectors, [
      { key: "industry", label: "Industry", text: true },
      { key: "period_end", label: "Quarter", text: true, format: fmt.quarter },
      { key: "weighted_mark", label: "Mark", format: fmt.mark },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "positions", label: "Positions", format: fmt.num },
    ], { sort: "fair_value" });

    table("table-markdowns", markdowns, [
      { key: "issuer_name", label: "Borrower", text: true },
      { key: "ticker", label: "BDC", text: true },
      { key: "investment_type", label: "Instrument", text: true, format: humanize },
      { key: "industry", label: "Industry", text: true },
      { key: "cost", label: "Cost", format: fmt.money },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "unrealized", label: "Unrealised", format: fmt.money },
      { key: "unrealized_pct", label: "vs cost", format: (v) => fmt.pct(v) },
      { key: "mark", label: "Mark", render: (row) => markCell(row.mark) },
    ], { sort: "unrealized", dir: "asc", limit: 200 });
  },

  async maturities() {
    const rows = await Data.get("maturity_wall");
    barChart(document.getElementById("chart-maturity"), rows, {
      x: "maturity_year", y: "fair_value", highlight: "stressed_fair_value",
      format: fmt.money, xLabel: (row) => row.maturity_year, label: "Debt fair value",
      tip: (row) => `<b>${row.maturity_year}</b><br>Fair value: <b>${fmt.money(row.fair_value)}</b>` +
        `<br>⚠ Stressed: <b>${fmt.money(row.stressed_fair_value || 0)}</b>` +
        `<br>${fmt.num(row.positions)} positions`,
    });
    table("table-maturity", rows, [
      { key: "maturity_year", label: "Year", text: true },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "principal", label: "Par", format: fmt.money },
      { key: "stressed_fair_value", label: "⚠ Stressed FV", format: fmt.money },
      { key: "nonaccrual_fair_value", label: "Non-accrual FV", format: fmt.money },
      { key: "positions", label: "Positions", format: fmt.num },
    ], { sort: "maturity_year", dir: "asc" });
  },

  async disagreements() {
    const rows = await Data.get("disagreements");
    table("table-disagreements", rows, [
      { key: "issuer_name", label: "Borrower", text: true },
      { key: "holders", label: "BDCs", format: fmt.num },
      { key: "spread", label: "Spread", title: "Highest mark minus lowest",
        format: (v) => v == null ? "–" : v.toFixed(1) },
      { key: "range", label: "Range 0–100", text: true,
        render: (row) => rangeBar(row.min_mark, row.max_mark, row.weighted_mark) },
      { key: "min_mark", label: "Low", format: fmt.mark },
      { key: "max_mark", label: "High", format: fmt.mark },
      { key: "weighted_mark", label: "Weighted", format: fmt.mark },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "marks_by_bdc", label: "Marks by BDC", text: true },
    ], { sort: "spread", limit: 300, empty: "No credits are held by more than one covered BDC yet." });
  },

  async positions() {
    const picker = document.getElementById("ticker");
    const ticker = picker.value || Data.meta.tickers[0];
    const rows = await Data.positions(ticker);
    const filter = document.getElementById("filter").value.trim().toLowerCase();
    const filtered = !filter ? rows : rows.filter((row) =>
      [row.issuer_name, row.industry, row.investment_type, row.facility]
        .some((value) => value && String(value).toLowerCase().includes(filter)));

    document.getElementById("position-count").textContent =
      `${fmt.num(filtered.length)} of ${fmt.num(rows.length)} positions`;

    table("table-positions", filtered, [
      { key: "issuer_name", label: "Borrower", text: true },
      { key: "investment_type", label: "Instrument", text: true, format: humanize },
      { key: "facility", label: "Facility", text: true, format: humanize },
      { key: "industry", label: "Industry", text: true },
      { key: "principal", label: "Par", format: fmt.money },
      { key: "cost", label: "Cost", format: fmt.money },
      { key: "fair_value", label: "Fair value", format: fmt.money },
      { key: "mark", label: "Mark", render: (row) => markCell(row.mark) },
      { key: "interest_rate", label: "Coupon", format: (v) => fmt.pct(v, 2) },
      { key: "spread", label: "Spread", format: (v) => fmt.pct(v, 2) },
      { key: "reference_rate", label: "Base", text: true },
      { key: "pik_rate", label: "PIK", format: (v) => v ? fmt.pct(v, 2) : "–" },
      { key: "maturity_date", label: "Maturity", text: true },
      { key: "is_non_accrual", label: "NA", text: true,
        render: (row) => row.is_non_accrual ? pill("non-accrual", "critical")
          : row.is_non_accrual === 0 ? "" : "n/d" },
      { key: "currency", label: "Ccy", text: true },
    ], { sort: "fair_value", limit: 1000 });
  },
};

/* ------------------------------------------------------------------ shell */

let current = "overview";

async function select(name) {
  current = name;
  for (const button of document.querySelectorAll("nav.tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.view === name));
  }
  for (const section of document.querySelectorAll("main section")) {
    section.hidden = section.id !== `view-${name}`;
  }
  hideTip();
  try {
    await views[name]();
  } catch (error) {
    console.error(error);
    document.querySelector(`#view-${name}`).insertAdjacentHTML("afterbegin",
      `<p class="empty">Could not load this view: ${error.message}</p>`);
  }
}

function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
  try {
    if (theme) localStorage.setItem("bdc-theme", theme);
    else localStorage.removeItem("bdc-theme");
  } catch (error) {
    /* private browsing; the toggle just will not persist */
  }
}

async function boot() {
  try {
    applyTheme(localStorage.getItem("bdc-theme"));
  } catch (error) {
    /* storage unavailable */
  }

  document.getElementById("theme").addEventListener("click", () => {
    const now = document.documentElement.getAttribute("data-theme");
    applyTheme(now === "dark" ? "light" : "dark");
  });

  await Data.init();
  Data.period = Data.meta.period_end;

  document.getElementById("subtitle").textContent =
    `${fmt.quarter(Data.meta.period_end)} · ${Data.meta.periods.length} quarters · ` +
    `source: ${Data.meta.sources.join(", ") || "none"}`;

  if (Data.meta.synthetic) {
    const banner = document.getElementById("banner");
    banner.hidden = false;
    banner.textContent =
      "⚠ SYNTHETIC DEMO DATA — every borrower, mark and figure on this page was " +
      "generated locally to develop the interface. Nothing here was extracted from an " +
      "SEC filing. Do not use for analysis. Run `bdc harvest` to load real marks.";
  }

  const period = document.getElementById("period");
  period.replaceChildren(...[...Data.meta.periods].reverse().map((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = fmt.quarter(value);
    return option;
  }));
  period.value = Data.meta.period_end;
  period.disabled = Data.mode !== "api";
  period.title = Data.mode === "api"
    ? "Choose a quarter"
    : "This build is frozen to one quarter; run `bdc serve` to switch periods.";
  period.addEventListener("change", () => {
    Data.period = period.value;
    Data.cache.clear();
    select(current);
  });

  const ticker = document.getElementById("ticker");
  ticker.replaceChildren(...[...Data.meta.tickers].sort().map((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    return option;
  }));
  ticker.addEventListener("change", () => views.positions());
  document.getElementById("filter").addEventListener("input", () => {
    if (current === "positions") views.positions();
  });

  for (const button of document.querySelectorAll("nav.tabs button")) {
    button.addEventListener("click", () => select(button.dataset.view));
  }

  await select("overview");
}

boot();
