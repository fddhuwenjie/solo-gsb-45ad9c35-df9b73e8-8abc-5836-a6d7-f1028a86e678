/* 公交时刻换乘规划台 —— 原生 JavaScript 前端 */
"use strict";

// ---------------------------------------------------------------- 全局状态
const EMPTY_DATA = { stations: [], trips: [], transfers: [], suspensions: [] };
let data = structuredClone(EMPTY_DATA);
let lastResult = null;
let stale = false;
let qid = 0;

const $ = (sel) => document.querySelector(sel);
const els = {
  stationsList: $("#stations-list"),
  tripsList: $("#trips-list"),
  transfersList: $("#transfers-list"),
  suspensionsList: $("#suspensions-list"),
  countStations: $("#count-stations"),
  origin: $("#q-origin"),
  destination: $("#q-destination"),
  earliest: $("#q-earliest"),
  preference: $("#q-preference"),
  result: $("#result"),
  recalc: $("#recalc-hint"),
  fileImport: $("#file-import"),
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const hhmm = (m) =>
  m == null ? "--:--" : `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// ---------------------------------------------------------------- 渲染：输入
function fieldHtml(label, inner) {
  return `<span class="field"><label>${label}</label>${inner}</span>`;
}

function stationOptions(skipId) {
  return data.stations
    .map((s) => `<option value="${esc(s.id)}">${esc(s.id)} · ${esc(s.name)}</option>`)
    .join("");
}

function routeOptions() {
  const routes = [...new Set(data.trips.map((t) => t.route).filter(Boolean))];
  return routes.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
}

function renderStations() {
  els.countStations.textContent = `共 ${data.stations.length} 个`;
  els.stationsList.innerHTML = data.stations
    .map(
      (s, i) => `
    <div class="row" data-k="stations" data-i="${i}">
      <div class="fields">
        ${fieldHtml("站点 ID", `<input class="w-id" data-f="id" value="${esc(s.id)}" />`)}
        ${fieldHtml("名称", `<input class="w-name" data-f="name" value="${esc(s.name)}" />`)}
      </div>
      <button class="del" type="button" data-del>删除</button>
    </div>`
    )
    .join("");
  syncSelects();
}

function renderTrips() {
  els.tripsList.innerHTML = data.trips
    .map((t, i) => {
      const stopsText = t.stops
        .map((s) => `${s.station} ${s.time}`)
        .join("\n");
      return `
    <div class="row" data-k="trips" data-i="${i}" style="align-items:flex-start;">
      <div class="fields">
        ${fieldHtml("班次 ID", `<input class="w-id" data-f="id" value="${esc(t.id)}" />`)}
        ${fieldHtml("线路名", `<input class="w-name" data-f="route" value="${esc(t.route)}" />`)}
        ${fieldHtml(
          "经停（每行：站点ID HH:MM）",
          `<textarea class="stops" data-f="stops" rows="3">${esc(stopsText)}</textarea>`
        )}
      </div>
      <button class="del" type="button" data-del>删除</button>
    </div>`;
    })
    .join("");
}

function renderTransfers() {
  els.transfersList.innerHTML = data.transfers
    .map(
      (x, i) => `
    <div class="row" data-k="transfers" data-i="${i}">
      <div class="fields">
        ${fieldHtml("从", `<select class="w-station" data-f="from">${stationOptions()}</select>`)}
        ${fieldHtml("到", `<select class="w-station" data-f="to">${stationOptions()}</select>`)}
        ${fieldHtml("步行分钟", `<input class="w-num" type="number" min="1" data-f="minutes" value="${esc(x.minutes)}" />`)}
      </div>
      <button class="del" type="button" data-del>删除</button>
    </div>`
    )
    .join("");
  // 恢复各自行内已选值（innerHTML 无法按行预置）
  els.transfersList.querySelectorAll(".row").forEach((row) => {
    const x = data.transfers[Number(row.dataset.i)];
    row.querySelector('[data-f="from"]').value = x.from;
    row.querySelector('[data-f="to"]').value = x.to;
  });
}

function renderSuspensions() {
  els.suspensionsList.innerHTML = data.suspensions
    .map(
      (x, i) => `
    <div class="row" data-k="suspensions" data-i="${i}">
      <div class="fields">
        ${fieldHtml(
          "类型",
          `<select data-f="kind">
             <option value="station">站点停运</option>
             <option value="route">线路整线停运</option>
             <option value="segment">线路区段停运</option>
           </select>`
        )}
        ${fieldHtml("站点", `<select class="w-station" data-f="stationRef">${stationOptions()}</select>`)}
        ${fieldHtml("线路名（或选择）",
          `<input class="w-name" list="route-list" data-f="routeRef" value="" placeholder="如 2路" />
           <datalist id="route-list">${routeOptions()}</datalist>`)}
        ${fieldHtml("区段起点站", `<select class="w-station" data-f="segFrom">${stationOptions()}</select>`)}
        ${fieldHtml("区段终点站", `<select class="w-station" data-f="segTo">${stationOptions()}</select>`)}
        ${fieldHtml("开始", `<input class="w-time" type="time" data-f="start" value="${esc(x.start)}" />`)}
        ${fieldHtml("结束", `<input class="w-time" type="time" data-f="end" value="${esc(x.end)}" />`)}
        ${fieldHtml("备注", `<input style="width:150px" data-f="label" value="${esc(x.label || "")}" />`)}
      </div>
      <button class="del" type="button" data-del>删除</button>
    </div>`
    )
    .join("");
  els.suspensionsList.querySelectorAll(".row").forEach((row) => {
    const x = data.suspensions[Number(row.dataset.i)];
    row.querySelector('[data-f="kind"]').value = x.kind;
    if (x.kind === "station") {
      row.querySelector('[data-f="stationRef"]').value = x.ref;
    } else if (x.kind === "route") {
      row.querySelector('[data-f="routeRef"]').value = x.ref;
    } else if (x.kind === "segment") {
      row.querySelector('[data-f="routeRef"]').value = x.route || "";
      row.querySelector('[data-f="segFrom"]').value = x.from;
      row.querySelector('[data-f="segTo"]').value = x.to;
    }
    applySuspKind(row);
  });
}

function applySuspKind(row) {
  const kind = row.querySelector('[data-f="kind"]').value;
  row.querySelector('[data-f="stationRef"]').parentElement.style.display =
    kind === "station" ? "" : "none";
  row.querySelector('[data-f="routeRef"]').parentElement.style.display =
    kind === "route" || kind === "segment" ? "" : "none";
  row.querySelector('[data-f="segFrom"]').parentElement.style.display =
    kind === "segment" ? "" : "none";
  row.querySelector('[data-f="segTo"]').parentElement.style.display =
    kind === "segment" ? "" : "none";
}

function renderAll() {
  renderStations();
  renderTrips();
  renderTransfers();
  renderSuspensions();
}

function syncSelects() {
  const prevO = els.origin.value;
  const prevD = els.destination.value;
  els.origin.innerHTML = stationOptions();
  els.destination.innerHTML = stationOptions();
  if ([...els.origin.options].some((o) => o.value === prevO)) els.origin.value = prevO;
  if ([...els.destination.options].some((o) => o.value === prevD)) els.destination.value = prevD;
  else if (data.stations.length)
    els.destination.value = data.stations[Math.min(1, data.stations.length - 1)].id;
  if (!els.origin.value && data.stations.length) els.origin.value = data.stations[0].id;
}

// ---------------------------------------------------------------- 输入变更
function readRow(row) {
  const k = row.dataset.k;
  const i = Number(row.dataset.i);
  const get = (f) => row.querySelector(`[data-f="${f}"]`);

  if (k === "stations") {
    data.stations[i] = {
      id: get("id").value.trim(),
      name: get("name").value.trim() || get("id").value.trim(),
    };
  } else if (k === "trips") {
    const stops = get("stops").value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const parts = line.split(/\s+/);
        return { station: parts[0] || "", time: parts[1] || "" };
      });
    data.trips[i] = {
      id: get("id").value.trim(),
      route: get("route").value.trim() || get("id").value.trim(),
      stops,
    };
  } else if (k === "transfers") {
    data.transfers[i] = {
      from: get("from").value,
      to: get("to").value,
      minutes: Number(get("minutes").value) || 1,
    };
  } else if (k === "suspensions") {
    const kind = get("kind").value;
    const rec = {
      kind,
      start: get("start").value || "00:00",
      end: get("end").value || "23:59",
      label: get("label").value.trim(),
    };
    if (kind === "station") {
      rec.ref = get("stationRef").value;
    } else if (kind === "route") {
      rec.ref = get("routeRef").value.trim();
    } else {
      // 线路区段停运：线路名 + 经停顺序中的起止站点 + 时间窗
      rec.route = get("routeRef").value.trim();
      rec.from = get("segFrom").value;
      rec.to = get("segTo").value;
    }
    data.suspensions[i] = rec;
  }
}

document.addEventListener("input", (ev) => {
  const row = ev.target.closest(".row");
  if (!row) return;
  readRow(row);
  if (row.dataset.k === "suspensions" && ev.target.dataset.f === "kind")
    applySuspKind(row);
  markStale();
});

// 站点/线路字段编辑完成（失焦）后再刷新依赖这些 ID 的下拉，避免逐字重绘导致丢焦点
document.addEventListener("change", (ev) => {
  const row = ev.target.closest(".row");
  if (!row) return;
  if (row.dataset.k === "stations" || row.dataset.k === "trips") {
    renderStations();
    renderTransfers();
    renderSuspensions();
  }
});

document.addEventListener("click", (ev) => {
  const delBtn = ev.target.closest("[data-del]");
  if (delBtn) {
    const row = delBtn.closest(".row");
    const k = row.dataset.k;
    data[k].splice(Number(row.dataset.i), 1);
    renderAll();
    markStale();
    return;
  }
  const addBtn = ev.target.closest("[data-add]");
  if (!addBtn) return;
  const what = addBtn.dataset.add;
  if (what === "station") {
    data.stations.push({ id: nextStationId(), name: "新站点" });
    renderStations();
    renderTransfers();
    renderSuspensions();
  } else if (what === "trip") {
    data.trips.push({
      id: nextTripId(),
      route: "新线路",
      stops: data.stations.length
        ? [{ station: data.stations[0].id, time: "08:00" }]
        : [],
    });
    renderTrips();
  } else if (what === "transfer") {
    const [a, b] = [data.stations[0], data.stations[1]];
    if (!a || !b) return showError("请先至少录入两个站点");
    data.transfers.push({ from: a.id, to: b.id, minutes: 5 });
    renderTransfers();
  } else if (what === "suspension") {
    data.suspensions.push({
      kind: "station",
      ref: data.stations[0]?.id || "",
      start: "12:00",
      end: "13:00",
      label: "",
    });
    renderSuspensions();
  }
  markStale();
});

function nextStationId() {
  let n = data.stations.length + 1;
  const used = new Set(data.stations.map((s) => s.id));
  while (used.has(`S${n}`)) n++;
  return `S${n}`;
}
function nextTripId() {
  let n = data.trips.length + 1;
  const used = new Set(data.trips.map((t) => t.id));
  while (used.has(`T${n}`)) n++;
  return `T${n}`;
}

// ---------------------------------------------------------------- 规划
function markStale() {
  stale = true;
  els.recalc.hidden = !lastResult;
}

async function runPlan() {
  // 保存行内尚未失焦的输入
  document.querySelectorAll(".row").forEach(readRow);

  const body = {
    data,
    query: {
      origin: els.origin.value,
      destination: els.destination.value,
      earliest: els.earliest.value || "00:00",
      preference: els.preference.value,
    },
  };
  els.result.innerHTML = `<p class="empty">搜索中…</p>`;
  let res;
  try {
    const resp = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    res = await resp.json();
  } catch (err) {
    return showError(`请求失败：${err.message}`);
  }
  if (res.status === "error") return showError(res.message);
  lastResult = res;
  stale = false;
  els.recalc.hidden = true;
  renderResult(res);
}

function showError(message) {
  els.result.innerHTML = `<p class="error-banner">⚠ ${esc(message)}</p>`;
}

function sname(sid) {
  const s = data.stations.find((x) => x.id === sid);
  return s ? `${s.id}·${s.name}` : sid;
}

function renderResult(res) {
  if (res.status === "blocked") {
    const b = res.blocker;
    els.result.innerHTML = `
      <div class="blocked">
        <span class="badge">无可行行程 · 首个阻塞约束</span>
        <p>${esc(b.message)}</p>
      </div>`;
    return;
  }
  const it = res.itinerary;
  const rideLegs = it.legs.filter((l) => l.type === "ride");
  const chips = [
    `<span class="chip ok">到达 <strong>${hhmm(it.arrival)}</strong></span>`,
    `<span class="chip">偏好：<strong>${esc(it.preference_label)}</strong></span>`,
    `<span class="chip">乘坐班次 <strong>${rideLegs.length}</strong></span>`,
    `<span class="chip">换乘 <strong>${it.transfers}</strong> 次</span>`,
    `<span class="chip">总步行 <strong>${it.walking_minutes}</strong> 分钟</span>`,
    `<span class="chip">总等待 <strong>${it.waiting_minutes}</strong> 分钟</span>`,
  ].join("");

  const legsHtml = it.legs
    .map((l) => {
      if (l.type === "wait") {
        return `<li class="leg wait">
          <span class="dot">等</span>
          <div class="when">${hhmm(l.start)} – ${hhmm(l.end)}（${l.minutes} 分钟）</div>
          <div class="what">站内候车</div>
          <div class="meta">在 ${esc(sname(l.station))} 等待下一班</div>
        </li>`;
      }
      if (l.type === "walk") {
        return `<li class="leg walk">
          <span class="dot">走</span>
          <div class="when">${hhmm(l.depart)} – ${hhmm(l.arrive)}（${l.minutes} 分钟）</div>
          <div class="what">步行换乘：${esc(sname(l.from))} → ${esc(sname(l.to))}</div>
          <div class="meta">步行 ${l.minutes} 分钟后于 ${hhmm(l.arrive)} 到达邻站方可乘车</div>
        </li>`;
      }
      return `<li class="leg ride">
        <span class="dot">乘</span>
        <div class="when">${hhmm(l.depart)} 发车 · ${hhmm(l.arrive)} 到达（${l.minutes} 分钟）</div>
        <div class="what">${esc(l.route)} · 班次 ${esc(l.trip_id)}</div>
        <div class="meta">${esc(sname(l.from))} 上车 → ${esc(sname(l.to))} 下车</div>
        ${l.truncated ? `<div class="meta trunc">⚠ ${esc(l.truncation)}</div>` : ""}
      </li>`;
    })
    .join("");

  els.result.innerHTML = `
    <div class="station-endpoint">
      ${esc(sname(it.origin))} → ${esc(sname(it.destination))}，
      不早于 ${hhmm(it.earliest)} 出发
    </div>
    <div class="summary">${chips}</div>
    <ul class="timeline">${legsHtml}</ul>`;
}

// ---------------------------------------------------------------- 工具按钮
async function loadSample() {
  const resp = await fetch("/api/sample");
  data = await resp.json();
  lastResult = null;
  renderAll();
  els.earliest.value = "08:00";
  els.preference.value = "fastest";
  els.result.innerHTML = `<p class="empty">示例数据已载入（含 8 个站点、5 个班次与 3 条停运），点击「规划行程」。</p>`;
  els.recalc.hidden = true;
}

function clearAll() {
  if (!confirm("确定清空全部站点、班次、换乘与停运记录？")) return;
  data = structuredClone(EMPTY_DATA);
  lastResult = null;
  renderAll();
  els.result.innerHTML = `<p class="empty">录入站点与班次后点击「规划行程」。</p>`;
  els.recalc.hidden = true;
}

function exportJson() {
  document.querySelectorAll(".row").forEach(readRow);
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "transit-data.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

els.fileImport.addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    data = {
      stations: parsed.stations || [],
      trips: parsed.trips || [],
      transfers: parsed.transfers || [],
      suspensions: parsed.suspensions || [],
    };
    renderAll();
    markStale();
  } catch (err) {
    showError(`导入失败：${err.message}`);
  }
  els.fileImport.value = "";
});

$("#btn-sample").addEventListener("click", loadSample);
$("#btn-clear").addEventListener("click", clearAll);
$("#btn-export").addEventListener("click", exportJson);
$("#btn-import").addEventListener("click", () => els.fileImport.click());
$("#btn-plan").addEventListener("click", runPlan);
[els.origin, els.destination, els.earliest, els.preference].forEach((el) =>
  el.addEventListener("change", markStale)
);

// ---------------------------------------------------------------- 初始化
renderAll();
loadSample();
