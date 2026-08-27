/* Hand-rolled SVG charts: no build step, no CDN, theme-aware through CSS vars.
   Every chart ships a hover layer and a table view, per the data-viz rules. */

const NS = "http://www.w3.org/2000/svg";

export const fmt = {
  money(value, digits = 1) {
    if (value == null || Number.isNaN(value)) return "–";
    const abs = Math.abs(value);
    if (abs >= 1e9) return `$${(value / 1e9).toFixed(digits)}bn`;
    if (abs >= 1e6) return `$${(value / 1e6).toFixed(digits)}m`;
    if (abs >= 1e3) return `$${(value / 1e3).toFixed(0)}k`;
    return `$${value.toFixed(0)}`;
  },
  pct(value, digits = 1) {
    return value == null || Number.isNaN(value) ? "–" : `${value.toFixed(digits)}%`;
  },
  mark(value) {
    return value == null || Number.isNaN(value) ? "–" : value.toFixed(1);
  },
  num(value) {
    return value == null || Number.isNaN(value) ? "–" : value.toLocaleString();
  },
  quarter(iso) {
    if (!iso) return "–";
    const [year, month] = iso.split("-").map(Number);
    return `${year}Q${Math.ceil(month / 3)}`;
  },
};

function el(name, attrs = {}, children = []) {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) node.append(child);
  return node;
}

let tooltipNode = null;
function tooltip() {
  if (!tooltipNode) {
    tooltipNode = document.createElement("div");
    tooltipNode.className = "tooltip";
    tooltipNode.hidden = true;
    document.body.append(tooltipNode);
  }
  return tooltipNode;
}

export function showTip(event, html) {
  const node = tooltip();
  node.innerHTML = html;
  node.hidden = false;
  const pad = 14;
  const rect = node.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = event.clientY - rect.height - pad;
  node.style.left = `${Math.max(8, x)}px`;
  node.style.top = `${Math.max(8, y)}px`;
}

export function hideTip() {
  tooltip().hidden = true;
}

function scaleLinear(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (value) => r0 + ((value - d0) / span) * (r1 - r0);
}

function niceTicks(min, max, count = 5) {
  const span = (max - min) || 1;
  const raw = span / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let value = start; value <= max + step / 1000; value += step) ticks.push(value);
  return ticks;
}

/** Line chart over quarters. One series, so no legend — the caption names it. */
export function lineChart(container, rows, options) {
  const { x, y, format = fmt.pct, label = "" } = options;
  const width = 720;
  const height = 240;
  const margin = { top: 12, right: 16, bottom: 28, left: 52 };
  const points = rows.filter((row) => row[y] != null);
  container.replaceChildren();
  if (points.length < 2) {
    container.append(Object.assign(document.createElement("p"), {
      className: "empty", textContent: "Not enough history yet.",
    }));
    return;
  }

  const values = points.map((row) => row[y]);
  const yMin = Math.min(...values);
  const yMax = Math.max(...values);
  const pad = (yMax - yMin || 1) * 0.15;
  const sx = scaleLinear([0, points.length - 1], [margin.left, width - margin.right]);
  const sy = scaleLinear([yMin - pad, yMax + pad], [height - margin.bottom, margin.top]);

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`,
    style: `max-width:${width}px`, role: "img", "aria-label": label });

  const grid = el("g", { class: "grid" });
  for (const tick of niceTicks(yMin - pad, yMax + pad)) {
    grid.append(el("line", { x1: margin.left, x2: width - margin.right, y1: sy(tick), y2: sy(tick) }));
    grid.append(el("text", { x: margin.left - 8, y: sy(tick) + 4, "text-anchor": "end" },
      [document.createTextNode(format(tick))]));
  }
  svg.append(grid);

  const path = points.map((row, i) => `${i ? "L" : "M"}${sx(i)},${sy(row[y])}`).join(" ");
  svg.append(el("path", { class: "line", d: path, stroke: "var(--series-1)" }));

  const axis = el("g", { class: "axis" });
  axis.append(el("line", { x1: margin.left, x2: width - margin.right,
    y1: height - margin.bottom, y2: height - margin.bottom }));
  points.forEach((row, i) => {
    axis.append(el("text", { x: sx(i), y: height - margin.bottom + 16, "text-anchor": "middle" },
      [document.createTextNode(fmt.quarter(row[x]))]));
  });
  svg.append(axis);

  // Direct label on the last point rather than a number on every point.
  const last = points[points.length - 1];
  svg.append(el("circle", { class: "dot", cx: sx(points.length - 1), cy: sy(last[y]),
    fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 }));
  svg.append(el("text", { class: "label", x: sx(points.length - 1) - 8, y: sy(last[y]) - 10,
    "text-anchor": "end" }, [document.createTextNode(format(last[y]))]));

  const crosshair = el("line", { class: "crosshair", y1: margin.top, y2: height - margin.bottom,
    opacity: 0 });
  const marker = el("circle", { class: "dot", fill: "var(--series-1)", stroke: "var(--surface)",
    "stroke-width": 2, opacity: 0 });
  svg.append(crosshair, marker);

  const hit = el("rect", { class: "hit", x: margin.left, y: margin.top,
    width: width - margin.left - margin.right, height: height - margin.top - margin.bottom });
  hit.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / box.width * width;
    const index = Math.max(0, Math.min(points.length - 1,
      Math.round((ratio - margin.left) / ((width - margin.right - margin.left) / (points.length - 1)))));
    const row = points[index];
    crosshair.setAttribute("x1", sx(index));
    crosshair.setAttribute("x2", sx(index));
    crosshair.setAttribute("opacity", 1);
    marker.setAttribute("cx", sx(index));
    marker.setAttribute("cy", sy(row[y]));
    marker.setAttribute("opacity", 1);
    showTip(event, `<b>${fmt.quarter(row[x])}</b><br>${label}: <b>${format(row[y])}</b>` +
      (options.extra ? `<br>${options.extra(row)}` : ""));
  });
  hit.addEventListener("pointerleave", () => {
    crosshair.setAttribute("opacity", 0);
    marker.setAttribute("opacity", 0);
    hideTip();
  });
  svg.append(hit);
  container.append(svg);
}

/** Vertical bars, optionally split into a base and a highlighted segment. */
export function barChart(container, rows, options) {
  const {
    x, y, highlight = null, format = fmt.money, xLabel = (row) => row[x],
    label = "", highlightLabel = "", baseColor = "var(--series-1)",
    highlightColor = "var(--critical)", tip = null,
  } = options;
  const width = 720;
  const height = 260;
  const margin = { top: 12, right: 16, bottom: 34, left: 60 };
  container.replaceChildren();
  if (!rows.length) {
    container.append(Object.assign(document.createElement("p"),
      { className: "empty", textContent: "No data for this period." }));
    return;
  }

  const yMax = Math.max(...rows.map((row) => row[y] || 0));
  const sy = scaleLinear([0, yMax || 1], [height - margin.bottom, margin.top]);
  const band = (width - margin.left - margin.right) / rows.length;
  // Thin marks: a 2px surface gap between adjacent bars, and a cap so a
  // seven-bar chart does not turn into seven slabs.
  const barWidth = Math.max(2, Math.min(48, band - 2));

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`,
    style: `max-width:${width}px`, role: "img", "aria-label": label });

  const grid = el("g", { class: "grid" });
  for (const tick of niceTicks(0, yMax || 1)) {
    grid.append(el("line", { x1: margin.left, x2: width - margin.right, y1: sy(tick), y2: sy(tick) }));
    grid.append(el("text", { x: margin.left - 8, y: sy(tick) + 4, "text-anchor": "end" },
      [document.createTextNode(format(tick, 0))]));
  }
  svg.append(grid);

  rows.forEach((row, i) => {
    const left = margin.left + i * band + (band - barWidth) / 2;
    const total = row[y] || 0;
    const base = el("rect", {
      x: left, y: sy(total), width: barWidth, height: Math.max(0, sy(0) - sy(total)),
      fill: baseColor, rx: 4,
    });
    svg.append(base);
    if (highlight && row[highlight]) {
      // Stressed slice sits on the baseline, separated by a 2px surface gap.
      const part = row[highlight];
      svg.append(el("rect", {
        x: left, y: sy(part), width: barWidth, height: Math.max(0, sy(0) - sy(part)),
        fill: highlightColor, rx: 4, stroke: "var(--surface)", "stroke-width": 2,
      }));
    }
    const target = el("rect", { x: left, y: margin.top, width: barWidth,
      height: height - margin.top - margin.bottom, fill: "transparent" });
    target.addEventListener("pointermove", (event) => showTip(event,
      tip ? tip(row) : `<b>${xLabel(row)}</b><br>${label}: <b>${format(total)}</b>`));
    target.addEventListener("pointerleave", hideTip);
    svg.append(target);
  });

  const axis = el("g", { class: "axis" });
  axis.append(el("line", { x1: margin.left, x2: width - margin.right,
    y1: sy(0), y2: sy(0) }));
  const stride = Math.ceil(rows.length / 14);
  rows.forEach((row, i) => {
    if (i % stride) return;
    axis.append(el("text", { x: margin.left + i * band + band / 2, y: height - margin.bottom + 16,
      "text-anchor": "middle" }, [document.createTextNode(String(xLabel(row)))]));
  });
  svg.append(axis);
  container.append(svg);
}

/** Sequential heatmap: rows x quarters, one hue light to dark. */
export function heatmap(container, rows, options) {
  const { row: rowKey, col: colKey, value: valueKey, format = fmt.mark, label = "" } = options;
  container.replaceChildren();
  if (!rows.length) {
    container.append(Object.assign(document.createElement("p"),
      { className: "empty", textContent: "No sector history yet." }));
    return;
  }

  const rowNames = [...new Set(rows.map((r) => r[rowKey]))].sort();
  const colNames = [...new Set(rows.map((r) => r[colKey]))].sort();
  const lookup = new Map(rows.map((r) => [`${r[rowKey]}|${r[colKey]}`, r]));
  const values = rows.map((r) => r[valueKey]).filter((v) => v != null);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const steps = ["var(--seq-100)", "var(--seq-250)", "var(--seq-400)", "var(--seq-550)", "var(--seq-700)"];

  const cell = 34;
  const left = 200;
  const top = 26;
  const width = left + colNames.length * cell + 12;
  const height = top + rowNames.length * cell + 8;
  const svg = el("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`,
    style: `max-width:${width}px`, role: "img", "aria-label": label });

  colNames.forEach((col, i) => {
    svg.append(el("text", { x: left + i * cell + cell / 2, y: top - 10, "text-anchor": "middle" },
      [document.createTextNode(fmt.quarter(col))]));
  });

  rowNames.forEach((name, r) => {
    svg.append(el("text", { class: "label", x: left - 10, y: top + r * cell + cell / 2 + 4,
      "text-anchor": "end" }, [document.createTextNode(name)]));
    colNames.forEach((col, c) => {
      const record = lookup.get(`${name}|${col}`);
      const value = record ? record[valueKey] : null;
      const ratio = value == null ? null : (value - min) / ((max - min) || 1);
      const fill = ratio == null ? "var(--surface-2)"
        : steps[Math.min(steps.length - 1, Math.floor(ratio * steps.length))];
      const rect = el("rect", {
        x: left + c * cell + 1, y: top + r * cell + 1, width: cell - 2, height: cell - 2,
        rx: 4, fill,
      });
      if (record) {
        rect.addEventListener("pointermove", (event) => showTip(event,
          `<b>${name}</b> · ${fmt.quarter(col)}<br>Weighted mark: <b>${format(value)}</b>` +
          `<br>${fmt.money(record.fair_value)} across ${fmt.num(record.positions)} positions`));
        rect.addEventListener("pointerleave", hideTip);
      }
      svg.append(rect);
    });
  });
  container.append(svg);
}
