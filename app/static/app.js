const state = {
  symbol: "AAPL",
  interval: "5m",
  days: 3,
  chartHasCandles: false,
  currentPrice: null,
  streamReconnectCooldownUntilMs: 0,
  streamReconnectLastAttemptMs: null,
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  const text = await res.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }

  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? (data.detail[0] && (data.detail[0].msg || JSON.stringify(data.detail[0])))
      : data.detail;
    throw new Error(detail || data.message || data.raw || `HTTP ${res.status}`);
  }
  return data;
}

function fmtNumber(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function fmtCompact(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString(undefined, {
    notation: "compact",
    maximumFractionDigits: 2,
  });
}

function fmtPercent(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const pct = Math.abs(n) <= 1 ? n * 100 : n;
  return `${fmtNumber(pct, digits)}%`;
}

function fmtSigned(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmtNumber(n, digits)}`;
}

function fmtAgeSeconds(v) {
  if (!isValidNumber(v)) return "—";
  const sec = Math.max(0, Number(v));
  if (sec < 60) return `${Math.round(sec)}s`;
  const minutes = Math.floor(sec / 60);
  const seconds = Math.round(sec % 60);
  return `${minutes}m ${seconds}s`;
}

function fmtLocalDateTime(ms) {
  if (!isValidNumber(ms)) return "Never";
  try {
    return new Date(Number(ms)).toLocaleString();
  } catch {
    return "Never";
  }
}

function fmtIsoDateTime(iso) {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "—";
  return dt.toLocaleString();
}

function isValidNumber(v) {
  return v !== null && v !== undefined && !Number.isNaN(Number(v));
}

function setSymbolPriceDisplay(price, source = "") {
  const el = document.getElementById("symbol-price-display");
  if (!el) return;
  if (!isValidNumber(price)) {
    el.textContent = "Current Price: —";
    return;
  }
  const sourceTag = source ? ` (${source})` : "";
  el.textContent = `Current Price: ${fmtNumber(price)}${sourceTag}`;
}

function renderCards(containerId, rows) {
  const root = document.getElementById(containerId);
  root.innerHTML = "";
  for (const row of rows) {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <div class="label">${row.label}</div>
      <div class="value">${row.value}</div>
      ${row.interp ? `<div class="interp">${row.interp}</div>` : ""}
    `;
    root.appendChild(div);
  }
}

function renderCandlestickRead(read) {
  const summary = document.getElementById("candlestick-read-summary");
  const interpretation = document.getElementById("candlestick-read-interpretation");
  const recommendation = document.getElementById("candlestick-read-recommendation");
  const recent = document.getElementById("candlestick-read-recent");
  if (!summary || !interpretation || !recommendation || !recent) return;

  if (!read) {
    summary.innerHTML = "";
    interpretation.textContent = "No candlestick interpretation available for this chart.";
    recommendation.textContent = "Recommendation unavailable.";
    recommendation.className = "info-box candlestick-reco neutral";
    recent.innerHTML = "<li>Recent candle details unavailable.</li>";
    return;
  }

  const pattern = read.pattern || "Unclassified";
  const bias = String(read.bias || "neutral").toLowerCase();
  const confidence = String(read.confidence || "low");

  renderCards("candlestick-read-summary", [
    { label: "Pattern", value: pattern },
    { label: "Bias", value: bias.toUpperCase() },
    { label: "Confidence", value: confidence.toUpperCase() },
  ]);

  interpretation.textContent = read.interpretation || "No pattern interpretation available.";
  recommendation.textContent = `Recommendation: ${read.recommendation || "Wait for clearer confirmation."}`;
  recommendation.className = `info-box candlestick-reco ${bias}`;

  recent.innerHTML = "";
  const rows = Array.isArray(read.recentCandles) ? read.recentCandles : [];
  if (!rows.length) {
    recent.innerHTML = "<li>Recent candle details unavailable.</li>";
    return;
  }
  for (const c of rows) {
    const li = document.createElement("li");
    const time = fmtIsoDateTime(c.timestamp);
    const close = fmtNumber(c.close);
    const delta = isValidNumber(c.changeFromOpenPct) ? fmtSigned(c.changeFromOpenPct) : "—";
    li.textContent = `${time}: ${c.candleType || "Candle"} | Close ${close} | Δ from open ${delta}%`;
    recent.appendChild(li);
  }
}

function renderActionBias(actionIndicator) {
  const el = document.getElementById("technicals-action-bias");
  if (!el) return;

  const interpretation = actionIndicator?.interpretation || "Action Bias unavailable.";
  const explanation = actionIndicator?.explanation || "";
  const fullText = `${interpretation}${explanation ? ` ${explanation}` : ""}`.trim();
  const lower = interpretation.toLowerCase();

  let tone = "neutral";
  if (lower.includes("bullish")) tone = "bullish";
  else if (lower.includes("bearish")) tone = "bearish";

  el.className = `info-box action-bias ${tone}`;
  el.textContent = fullText;
}

function renderStreamDiagnostics(health) {
  const grid = document.getElementById("stream-diagnostics-grid");
  const errorBox = document.getElementById("stream-diagnostics-error");
  if (!grid || !errorBox) return;

  const enabled = Boolean(health.schwabStreamEnabled);
  const connected = Boolean(health.schwabStreamConnected);
  const stateText = !enabled ? "Disabled" : (connected ? "Connected" : "Disconnected");
  const trackedSymbols = isValidNumber(health.schwabStreamTrackedSymbols)
    ? fmtNumber(health.schwabStreamTrackedSymbols, 0)
    : "—";
  const messageAge = fmtAgeSeconds(health.schwabStreamLastMessageAgeSec);
  const cooldownRemainingSec = Math.max(0, Math.ceil((state.streamReconnectCooldownUntilMs - Date.now()) / 1000));
  const cooldownText = cooldownRemainingSec > 0 ? `${cooldownRemainingSec}s` : "Ready";
  const lastAttemptText = fmtLocalDateTime(state.streamReconnectLastAttemptMs);

  const rows = [
    { label: "State", value: stateText },
    { label: "Tracked Symbols", value: trackedSymbols },
    { label: "Last Message Age", value: messageAge },
    { label: "Reconnect Cooldown", value: cooldownText },
    { label: "Last Reconnect Attempt", value: lastAttemptText },
    { label: "Schwab Config", value: health.schwabEnabled ? "Configured" : "Not configured" },
  ];

  grid.innerHTML = rows
    .map((r) => `
      <div class="diag-item">
        <div class="diag-label">${r.label}</div>
        <div class="diag-value">${r.value}</div>
      </div>
    `)
    .join("");

  const err = health.schwabStreamLastError;
  if (err) {
    errorBox.textContent = `Last stream error: ${err}`;
  } else if (!enabled) {
    errorBox.textContent = "Stream diagnostics: Schwab streaming is disabled.";
  } else if (!connected) {
    errorBox.textContent = "No active stream connection yet. Waiting for login/subscription.";
  } else {
    errorBox.textContent = "No stream errors reported.";
  }
}

function updateReconnectButton() {
  const btn = document.getElementById("stream-restart-btn");
  if (!btn) return;
  const cooldownRemainingSec = Math.max(0, Math.ceil((state.streamReconnectCooldownUntilMs - Date.now()) / 1000));
  if (cooldownRemainingSec > 0) {
    btn.disabled = true;
    btn.textContent = `Reconnect Stream (${cooldownRemainingSec}s)`;
  } else {
    btn.disabled = false;
    btn.textContent = "Reconnect Stream";
  }
}

async function loadHealth() {
  const health = await api("/api/health");
  const streamState = health.schwabStreamEnabled
    ? (health.schwabStreamConnected ? "Connected" : "Disconnected")
    : "Disabled";
  document.getElementById("health-status").textContent =
    `API: ${health.status} | Schwab: ${health.schwabEnabled ? "Configured" : "Not configured"} | Stream: ${streamState}`;
  document.getElementById("scanner-interval").value = health.scannerIntervalSec;
  document.getElementById("scanner-market-cap").value = health.scannerMarketCapMin ?? 2000000000;
  document.getElementById("scanner-avg-volume").value = health.scannerAvgVolumeMin ?? 500000;
  document.getElementById("scanner-rel-volume").value = health.scannerRelativeVolumeMin ?? 1.5;
  renderStreamDiagnostics(health);
}

async function reconnectStream() {
  const btn = document.getElementById("stream-restart-btn");
  const now = Date.now();
  const cooldownRemainingSec = Math.max(0, Math.ceil((state.streamReconnectCooldownUntilMs - now) / 1000));
  if (cooldownRemainingSec > 0) {
    document.getElementById("health-status").textContent =
      `Reconnect on cooldown: wait ${cooldownRemainingSec}s before retrying.`;
    updateReconnectButton();
    return;
  }

  state.streamReconnectLastAttemptMs = now;
  state.streamReconnectCooldownUntilMs = now + 15000;
  updateReconnectButton();
  if (btn) btn.disabled = true;
  try {
    const result = await api("/api/stream/restart", { method: "POST" });
    const statusText = result?.status === "ok" ? "Stream reconnect requested." : `Stream reconnect skipped: ${result?.reason || "unknown reason"}.`;
    document.getElementById("health-status").textContent = statusText;
  } catch (err) {
    document.getElementById("health-status").textContent = `Stream reconnect failed: ${err.message}`;
  } finally {
    await loadHealth();
    updateReconnectButton();
  }
}

async function loadScanner() {
  const snapshot = await api("/api/scanner/results");
  const body = document.querySelector("#scanner-table tbody");
  body.innerHTML = "";
  const allRows = snapshot.candidates || [];
  const rows = allRows.filter((r) => r.passed);

  for (const c of rows) {
    const moveClass = isValidNumber(c.day_change)
      ? (Number(c.day_change) > 0 ? "pass" : (Number(c.day_change) < 0 ? "fail" : ""))
      : "";
    let moveText = "—";
    if (isValidNumber(c.day_change) && isValidNumber(c.day_change_percent)) {
      moveText = `${fmtSigned(c.day_change)} (${fmtSigned(c.day_change_percent)}%)`;
    } else if (isValidNumber(c.day_change)) {
      moveText = `${fmtSigned(c.day_change)} (—)`;
    } else if (isValidNumber(c.day_change_percent)) {
      moveText = `— (${fmtSigned(c.day_change_percent)}%)`;
    }
    const tr = document.createElement("tr");
    const triggeredAt = c.triggered_at
      ? new Date(c.triggered_at).toLocaleTimeString()
      : "—";
    tr.innerHTML = `
      <td><button data-symbol="${c.symbol}" class="pick-symbol-btn">${c.symbol}</button></td>
      <td>${fmtNumber(c.price)}</td>
      <td>${fmtCompact(c.market_cap)}</td>
      <td>${fmtCompact(c.avg_volume)}</td>
      <td>${fmtNumber(c.relative_volume, 2)}</td>
      <td class="${c.passed ? "pass" : "fail"}">${c.passed ? "PASS" : "FAIL"}</td>
      <td>${(c.reasons || []).join("<br/>")}</td>
      <td class="${moveClass}">${moveText}</td>
      <td>${triggeredAt}</td>
    `;
    body.appendChild(tr);
  }
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="9" class="muted">No symbols currently pass all scanner criteria.</td>`;
    body.appendChild(tr);
  }

  const generated = new Date(snapshot.generated_at).toLocaleTimeString();
  const marketCapMin = snapshot.market_cap_min ?? Number(document.getElementById("scanner-market-cap").value || 2000000000);
  const avgVolumeMin = snapshot.avg_volume_min ?? Number(document.getElementById("scanner-avg-volume").value || 500000);
  const rvolMin = snapshot.relative_volume_min ?? Number(document.getElementById("scanner-rel-volume").value || 1.5);
  document.getElementById("scanner-meta").textContent =
    `Last scan: ${generated} | Interval: ${snapshot.interval_sec}s | Min Cap: ${fmtCompact(marketCapMin)} | Min Avg Vol: ${fmtCompact(avgVolumeMin)} | Min RVOL: ${fmtNumber(rvolMin, 2)} | Matches: ${rows.length}/${allRows.length}`;
}

async function runScannerNow() {
  await api("/api/scanner/run", { method: "POST" });
  await loadScanner();
}

async function updateScannerInterval() {
  const value = Number(document.getElementById("scanner-interval").value || 45);
  await api(`/api/scanner/interval/${value}`, { method: "POST" });
  await loadScanner();
}

async function updateScannerCriteria() {
  const marketCapMin = Number(document.getElementById("scanner-market-cap").value || 2000000000);
  const avgVolumeMin = Number(document.getElementById("scanner-avg-volume").value || 500000);
  const rvolMin = Number(document.getElementById("scanner-rel-volume").value || 1.5);
  const query = new URLSearchParams({
    market_cap_min: String(marketCapMin),
    avg_volume_min: String(avgVolumeMin),
    relative_volume_min: String(rvolMin),
  });
  await api(`/api/scanner/criteria?${query.toString()}`, { method: "POST" });
  await loadScanner();
}

async function loadFundamentals() {
  const payload = await api(`/api/fundamentals/${state.symbol}`);
  const f = payload.fundamentals || {};
  if (isValidNumber(f.price)) {
    state.currentPrice = Number(f.price);
    setSymbolPriceDisplay(state.currentPrice, "fundamentals");
  } else if (!isValidNumber(state.currentPrice)) {
    setSymbolPriceDisplay(null);
  }

  renderCards("fundamentals-cards", [
    { label: "Current Price", value: fmtNumber(f.price) },
    { label: "Market Cap", value: fmtCompact(f.marketCap) },
    { label: "Shares Outstanding", value: fmtCompact(f.sharesOutstanding) },
    { label: "Beta", value: fmtNumber(f.beta) },
    { label: "Trailing P/E", value: fmtNumber(f.trailingPE) },
    { label: "EPS (TTM)", value: fmtNumber(f.trailingEps) },
    { label: "Revenue (TTM)", value: fmtCompact(f.revenueTTM) },
    { label: "Profit Margin", value: fmtPercent(f.profitMargin) },
    { label: "Operating Margin", value: fmtPercent(f.operatingMargin) },
    { label: "Gross Margin", value: fmtPercent(f.grossMargin) },
    { label: "Insider Ownership", value: fmtPercent(f.insiderOwnership) },
    { label: "Institutional Ownership", value: fmtPercent(f.institutionalOwnership) },
    { label: "Short Float", value: fmtPercent(f.shortFloat) },
    { label: "Short Ratio", value: fmtNumber(f.shortRatio) },
    { label: "52W High", value: fmtNumber(f.fiftyTwoWeekHigh) },
    { label: "52W Low", value: fmtNumber(f.fiftyTwoWeekLow) },
    { label: "Avg Volume", value: fmtCompact(f.averageVolume) },
    { label: "Current Volume", value: fmtCompact(f.currentVolume) },
    { label: "Relative Volume", value: fmtNumber(f.relativeVolume) },
    { label: "Next Earnings", value: f.nextEarningsDate || "N/A" },
  ]);

  const filingList = document.getElementById("filings-list");
  filingList.innerHTML = "";
  for (const filing of payload.filings_8k || []) {
    const li = document.createElement("li");
    li.innerHTML = `<a href="${filing.url}" target="_blank" rel="noreferrer">${filing.filingDate} - ${filing.form}</a>`;
    filingList.appendChild(li);
  }
  if (!filingList.innerHTML) filingList.innerHTML = "<li>No recent 8-K filings found.</li>";

  const sentiment = payload.sentiment || {};
  const dataError = f.dataError ? `<div style="color:#fca5a5">Data source warning: ${f.dataError}</div>` : "";
  const dataWarning = f.dataWarning ? `<div style="color:#fcd34d">Data source note: ${f.dataWarning}</div>` : "";
  const insiderEstimate = f.insiderOwnershipEstimated
    ? `<div style="color:#93c5fd">Insider ownership is an estimate from float shares when direct holder data is unavailable.</div>`
    : "";
  document.getElementById("sentiment-box").innerHTML = `
    ${dataError}
    ${dataWarning}
    ${insiderEstimate}
    <div>Buzz Score: ${fmtNumber(sentiment.buzz?.buzz)}</div>
    <div>Bullish: ${fmtNumber((sentiment.sentiment?.bullishPercent || 0))}%</div>
    <div>Bearish: ${fmtNumber((sentiment.sentiment?.bearishPercent || 0))}%</div>
    <div>Company: ${f.longName || state.symbol}</div>
  `;

  const newsList = document.getElementById("news-list");
  newsList.innerHTML = "";
  for (const item of payload.news || []) {
    const dt = item.datetime ? new Date(item.datetime * 1000).toLocaleString() : "";
    const li = document.createElement("li");
    li.innerHTML = `<a href="${item.url}" target="_blank" rel="noreferrer">${item.headline || "News"}</a> <span class="muted">(${dt})</span>`;
    newsList.appendChild(li);
  }
  if (!newsList.innerHTML) newsList.innerHTML = "<li>No recent headlines returned.</li>";
}

async function loadTechnicals() {
  const payload = await api(`/api/technicals/${state.symbol}?interval=${state.interval}&days=${state.days}`);
  const indicators = payload.indicators || [];
  const actionBias = indicators.find((i) => i.key === "Action Bias");
  renderActionBias(actionBias);
  renderCards(
    "technicals-cards",
    indicators
      .filter((i) => i.key !== "Action Bias")
      .map((i) => ({
      label: i.key,
      value: i.value === null || i.value === undefined ? "—" : fmtNumber(i.value),
      interp: `${i.interpretation} ${i.explanation}`,
      }))
  );

  const signalList = document.getElementById("signal-list");
  signalList.innerHTML = "";
  for (const s of payload.signals || []) {
    const li = document.createElement("li");
    li.textContent = `${s.name}: ${s.interpretation} Action: ${s.prediction}`;
    signalList.appendChild(li);
  }
  if (!signalList.innerHTML) signalList.innerHTML = "<li>No active structure signals detected.</li>";
  return payload;
}

async function loadChart() {
  const payload = await api(`/api/chart/${state.symbol}?interval=${state.interval}&days=${state.days}`);
  const figure = payload.figure || {};
  Plotly.newPlot("chart", figure.data || [], figure.layout || {}, { responsive: true });
  state.chartHasCandles = Boolean(payload.candles && payload.candles.length > 0);
  renderCandlestickRead(payload.candlestickRead || null);
  return payload;
}

async function loadQuote() {
  const quote = await api(`/api/quote/${state.symbol}`);
  if (isValidNumber(quote.last)) {
    state.currentPrice = Number(quote.last);
    setSymbolPriceDisplay(state.currentPrice, quote.source || "quote");
  } else if (!isValidNumber(state.currentPrice)) {
    setSymbolPriceDisplay(null);
  }
  renderCards("quote-box", [
    { label: "Source", value: quote.source || "unknown" },
    { label: "Bid", value: fmtNumber(quote.bid) },
    { label: "Ask", value: fmtNumber(quote.ask) },
    { label: "Last", value: fmtNumber(quote.last) },
    { label: "Bid Size", value: fmtNumber(quote.bidSize, 0) },
    { label: "Ask Size", value: fmtNumber(quote.askSize, 0) },
  ]);
}

async function submitTrade(event) {
  event.preventDefault();
  const form = event.target;
  const payload = {
    symbol: state.symbol,
    side: form.side.value,
    quantity: Number(form.quantity.value),
    order_type: form.order_type.value,
    limit_price: form.limit_price.value ? Number(form.limit_price.value) : null,
    stop_loss: form.stop_loss.value ? Number(form.stop_loss.value) : null,
    take_profit: form.take_profit.value ? Number(form.take_profit.value) : null,
    strategy: form.strategy.value || "manual",
    dry_run: Boolean(form.dry_run.checked),
  };
  try {
    const result = await api("/api/trades/order", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    document.getElementById("trade-result").textContent = JSON.stringify(result, null, 2);
    if (!payload.dry_run) {
      await syncTrades();
    }
    await loadJournal();
  } catch (err) {
    document.getElementById("trade-result").textContent = err.message;
  }
}

async function closeTrade(tradeId) {
  const raw = prompt(`Exit price for trade ${tradeId}:`);
  if (!raw) return;
  const exitPrice = Number(raw);
  if (Number.isNaN(exitPrice)) return;
  await api("/api/trades/close", {
    method: "POST",
    body: JSON.stringify({ trade_id: tradeId, exit_price: exitPrice, notes: "" }),
  });
  await loadJournal();
}

async function loadJournal() {
  const [journal, perf] = await Promise.all([
    api("/api/trades/journal"),
    api("/api/trades/performance"),
  ]);

  renderCards("perf-box", [
    { label: "Closed Trades", value: perf.totalTrades || 0 },
    { label: "Pending Orders", value: journal.pendingCount || 0 },
    { label: "Win Rate", value: `${fmtNumber((perf.winRate || 0) * 100)}%` },
    { label: "Wins / Losses", value: `${perf.wins || 0} / ${perf.losses || 0}` },
    { label: "Net P&L", value: fmtNumber(perf.netPnl || 0) },
  ]);

  const tbody = document.querySelector("#trades-table tbody");
  tbody.innerHTML = "";

  const tradeDisplayTime = (trade) =>
    (trade.status === "closed" ? (trade.closed_at || null) : null) ||
    trade.updated_at ||
    trade.opened_at ||
    null;

  const sortedTrades = (journal.trades || []).slice().sort((a, b) => {
    const aMs = Date.parse(tradeDisplayTime(a) || "");
    const bMs = Date.parse(tradeDisplayTime(b) || "");
    const aSafe = Number.isFinite(aMs) ? aMs : Number.NEGATIVE_INFINITY;
    const bSafe = Number.isFinite(bMs) ? bMs : Number.NEGATIVE_INFINITY;
    return bSafe - aSafe;
  });

  for (const t of sortedTrades.slice(0, 100)) {
    const tr = document.createElement("tr");
    const closeBtn = t.status === "open"
      ? `<button class="close-trade-btn" data-trade-id="${t.trade_id}">Close</button>`
      : "";
    const rowDateTime = tradeDisplayTime(t);
    tr.innerHTML = `
      <td>${t.trade_id || ""}</td>
      <td>${fmtIsoDateTime(rowDateTime)}</td>
      <td>${t.symbol || ""}</td>
      <td>${t.quantity || ""}</td>
      <td>${fmtNumber(t.entry_price)}</td>
      <td>${fmtNumber(t.exit_price)}</td>
      <td>${t.status || ""} ${closeBtn}</td>
      <td>${t.broker_status || "—"}</td>
      <td>${fmtNumber(t.pnl)}</td>
      <td>${t.strategy || ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function syncTrades() {
  try {
    const result = await api("/api/trades/sync", { method: "POST" });
    const updateCount = (result.updates || []).length;
    const errorCount = (result.errors || []).length;
    const importedCount = Number(result.historyImport?.importedClosedTrades || 0);
    const loggedOrderCount = Number(result.historyImport?.loggedOrders || 0);
    const backfilledTsCount = Number(result.historyImport?.backfilledTradeTimestamps || 0);
    document.getElementById("trade-result").textContent =
      `Sync complete. Updates: ${updateCount}, Imported closed trades: ${importedCount}, Timestamp backfills: ${backfilledTsCount}, Imported orders: ${loggedOrderCount}, Errors: ${errorCount}`;
    await loadJournal();
  } catch (err) {
    document.getElementById("trade-result").textContent = `Sync failed: ${err.message}`;
  }
}

async function loadSymbolWorkspace() {
  state.symbol = document.getElementById("symbol-input").value.trim().toUpperCase() || "AAPL";
  state.interval = document.getElementById("interval-select").value;
  state.days = Number(document.getElementById("days-input").value || 3);
  state.currentPrice = null;
  setSymbolPriceDisplay(null);

  const stageOne = await Promise.allSettled([loadFundamentals(), loadQuote()]);
  const chartResult = await Promise.allSettled([loadChart()]);
  let technicalPayload = null;
  try {
    technicalPayload = await loadTechnicals();
  } catch {
    technicalPayload = null;
  }

  // If technicals reported no bars but chart has bars, retry technicals once.
  const noBars =
    technicalPayload &&
    Array.isArray(technicalPayload.indicators) &&
    technicalPayload.indicators.length === 1 &&
    technicalPayload.indicators[0]?.key === "Data Source Status";
  if (noBars && state.chartHasCandles) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    try {
      await loadTechnicals();
    } catch {
      // Keep first result if retry fails.
    }
  }

  const results = [...stageOne, ...chartResult];
  const failures = results.filter((r) => r.status === "rejected");
  if (failures.length) {
    document.getElementById("health-status").textContent =
      `Partial data load (${failures.length} source${failures.length > 1 ? "s" : ""} unavailable).`;
  }
}

function wireEvents() {
  document.getElementById("scan-now-btn").addEventListener("click", runScannerNow);
  document.getElementById("update-interval-btn").addEventListener("click", updateScannerInterval);
  document.getElementById("update-scanner-criteria-btn").addEventListener("click", updateScannerCriteria);
  document.getElementById("load-symbol-btn").addEventListener("click", loadSymbolWorkspace);
  document.getElementById("refresh-quote-btn").addEventListener("click", loadQuote);
  document.getElementById("refresh-journal-btn").addEventListener("click", loadJournal);
  document.getElementById("sync-trades-btn").addEventListener("click", syncTrades);
  document.getElementById("trade-form").addEventListener("submit", submitTrade);
  document.getElementById("stream-restart-btn").addEventListener("click", reconnectStream);

  document.body.addEventListener("click", async (event) => {
    const pickBtn = event.target.closest(".pick-symbol-btn");
    if (pickBtn) {
      document.getElementById("symbol-input").value = pickBtn.dataset.symbol;
      await loadSymbolWorkspace();
      return;
    }
    const closeBtn = event.target.closest(".close-trade-btn");
    if (closeBtn) {
      await closeTrade(closeBtn.dataset.tradeId);
    }
  });
}

async function boot() {
  wireEvents();
  await loadHealth();
  updateReconnectButton();
  const bootResults = await Promise.allSettled([loadScanner(), loadSymbolWorkspace(), loadJournal()]);
  const failures = bootResults.filter((r) => r.status === "rejected");
  if (failures.length) {
    document.getElementById("health-status").textContent =
      `App loaded with ${failures.length} startup warning${failures.length > 1 ? "s" : ""}.`;
  }
  setInterval(loadHealth, 10000);
  setInterval(updateReconnectButton, 1000);
  setInterval(loadScanner, 30000);
  setInterval(loadQuote, 5000);
  setInterval(syncTrades, 20000);
}

boot().catch((err) => {
  document.getElementById("health-status").textContent = `Startup error: ${err.message}`;
});
