"use strict";

const yen = (v) =>
  v === null || v === undefined ? "-" : Math.round(v).toLocaleString("ja-JP") + " 円";
const pct = (v) =>
  v === null || v === undefined ? "-" : (v * 100).toFixed(1) + " %";
const sign = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const fmtTs = (s) => (s ? s.replace("T", " ").slice(0, 16).replace("+00:00", "") : "-");

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}

let equityChart, dailyChart, signalChart;

const STATUS_LABEL = { ok: "OK", warn: "要確認", fail: "NG", info: "参考" };

async function renderReconciliation() {
  const d = await getJSON("/api/reconciliation");
  const v = document.getElementById("reconVerdict");
  v.textContent =
    `${STATUS_LABEL[d.verdict] || d.verdict} — ${d.headline}` +
    `(NG ${d.tally.fail} / 要確認 ${d.tally.warn} / OK ${d.tally.ok})`;
  v.className = "verdict " + d.verdict;

  document.getElementById("reconChecks").innerHTML = d.checks
    .map((c) => {
      const exp = typeof c.expected === "object" ? JSON.stringify(c.expected) : c.expected;
      const act = typeof c.actual === "object" ? JSON.stringify(c.actual) : c.actual;
      return `<div class="check ${c.status}">
        <div class="check-top">
          <span class="check-badge ${c.status}">${STATUS_LABEL[c.status] || c.status}</span>
          <span class="check-label">${c.label}</span>
        </div>
        <div class="check-vals"><span>実測: ${act}</span><span>期待: ${exp}</span></div>
        <div class="check-note">${c.note || ""}</div>
      </div>`;
    })
    .join("");

  document.getElementById("reconBaseline").textContent = JSON.stringify(d.baseline, null, 2);
}

async function renderParity() {
  const both = await getJSON("/api/backtest-parity");
  renderSignalParity(both.signal || { available: false });
  renderTradeParity(both.trade || { available: false });
}

function renderSignalParity(d) {
  const rate = document.getElementById("parityRate");
  const meta = document.getElementById("parityMeta");
  if (!d.available) {
    rate.textContent = "未実行";
    rate.className = "verdict info";
    meta.textContent =
      "`python -m backtest.replay --yfinance --start <運用開始> --end <now>` を実行すると、" +
      "バックテスト戦略の entry シグナルと実運用シグナルを 1 件ずつ突合します。";
    ["parityCounts", "parityByStrategy"].forEach((i) => (document.getElementById(i).innerHTML = ""));
    document.getElementById("paritySamples").textContent = "";
    return;
  }
  const r = d.match_rate;
  rate.textContent = `一致率 ${(r * 100).toFixed(1)}%`;
  rate.className = "verdict " + (r >= 0.9 ? "ok" : r >= 0.75 ? "warn" : "fail");
  meta.textContent =
    `期待シグナル ${d.expected_n} / 実運用 ${d.actual_n}・` +
    `OHLC: ${d.ohlc_source}(${d.bars} 本、${fmtTs(d.period_start)}〜${fmtTs(d.period_end)})・実行 ${fmtTs(d.ran_at)}`;

  document.getElementById("parityCounts").innerHTML = [
    ["MATCHED", d.matched_n, "pos"],
    ["MISSING(取り逃し)", d.missing_n, "neg"],
    ["EXTRA(余分な発注)", d.extra_n, "neg"],
    ["MISMATCH(方向逆)", d.mismatch_n, "neg"],
  ]
    .map(([l, v, c]) => `<div class="kpi"><div class="label">${l}</div><div class="value ${v ? c : ""}">${v}</div></div>`)
    .join("");

  const bs = (d.detail && d.detail.by_strategy) || [];
  table(
    "parityByStrategy",
    ["手法", "期待", "一致", "取り逃し", "余分", "方向逆", "一致率"],
    bs.map((x) => [x.strategy, x.expected, x.matched, x.missing, x.extra, x.mismatch,
      x.match_rate == null ? "-" : (x.match_rate * 100).toFixed(0) + "%"])
  );
  document.getElementById("paritySamples").textContent = JSON.stringify((d.detail && d.detail.samples) || {}, null, 2);
}

function renderTradeParity(d) {
  const rate = document.getElementById("tradeParityRate");
  const meta = document.getElementById("tradeParityMeta");
  if (!d.available) {
    rate.textContent = "未実行";
    rate.className = "verdict info";
    meta.textContent =
      "`python -m backtest.replay --yfinance --mode trade ...` で、エグジット(SL/TP/トレール)を再現した" +
      "「バックテストなら成立したトレード」と実 trades を突合し、勝敗・決済理由・保有時間を比較します。";
    ["tradeParityCounts", "tradeParityByStrategy"].forEach((i) => (document.getElementById(i).innerHTML = ""));
    document.getElementById("tradeParitySamples").textContent = "";
    return;
  }
  const r = d.match_rate;
  const agg = (d.detail && d.detail.aggregate) || {};
  const cov = agg.coverage;
  rate.textContent = `勝敗一致率 ${(r * 100).toFixed(1)}%(マッチ分)`;
  rate.className = "verdict " + (r >= 0.8 ? "ok" : r >= 0.6 ? "warn" : "fail");
  meta.textContent =
    `バックテスト想定 ${d.expected_n} トレード・カバレッジ ${cov == null ? "-" : (cov * 100).toFixed(0) + "%"}` +
    `(実際に取れた ${d.matched_n})・平均R sim=${agg.avg_r_sim ?? "-"} vs 実=${agg.avg_r_actual_matched ?? "-"}・` +
    `決済理由一致率 ${agg.reason_agree_rate == null ? "-" : (agg.reason_agree_rate * 100).toFixed(0) + "%"}・` +
    `OHLC: ${d.ohlc_source}・実行 ${fmtTs(d.ran_at)}`;

  document.getElementById("tradeParityCounts").innerHTML = [
    ["MATCHED", d.matched_n, "pos"],
    ["MISSING(取れていた)", d.missing_n, "neg"],
    ["EXTRA(想定外の実トレード)", d.extra_n, "neg"],
    ["勝敗不一致", d.mismatch_n, "neg"],
  ]
    .map(([l, v, c]) => `<div class="kpi"><div class="label">${l}</div><div class="value ${v ? c : ""}">${v}</div></div>`)
    .join("");

  const bs = (d.detail && d.detail.by_strategy) || [];
  table(
    "tradeParityByStrategy",
    ["手法", "一致", "勝敗一致", "理由一致", "取り逃し", "想定外", "勝敗一致率"],
    bs.map((x) => [x.strategy, x.matched, x.outcome_agree, x.reason_agree, x.missing, x.extra,
      x.outcome_agree_rate == null ? "-" : (x.outcome_agree_rate * 100).toFixed(0) + "%"])
  );
  document.getElementById("tradeParitySamples").textContent = JSON.stringify((d.detail && d.detail.samples) || {}, null, 2);
}

function renderKPI(s) {
  const k = s.kpi;

  const banner = document.getElementById("demoBanner");
  const isDemo = typeof s.data_mode === "string" && s.data_mode.startsWith("demo");
  banner.hidden = !isDemo;
  if (isDemo) document.getElementById("demoBannerMode").textContent = "(" + s.data_mode + ")";
  const cards = [
    ["累積損益", yen(k.net_pnl_jpy), sign(k.net_pnl_jpy)],
    ["勝率", pct(k.win_rate), ""],
    ["プロフィットファクター", k.profit_factor ?? "-", ""],
    ["最大ドローダウン", (k.max_drawdown_pct ?? 0).toFixed(1) + " %", "neg"],
    ["総取引数(決済済)", k.total_trades, ""],
    ["オープン中", k.open_positions, ""],
    ["期待値/取引", yen(k.expectancy_jpy), sign(k.expectancy_jpy)],
    ["平均利益 / 平均損失", yen(k.avg_win_jpy) + " / " + yen(k.avg_loss_jpy), ""],
  ];
  document.getElementById("kpiGrid").innerHTML = cards
    .map(
      ([label, value, cls]) =>
        `<div class="kpi"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`
    )
    .join("");

  // ボット死活
  const dot = document.getElementById("botDot");
  dot.className = "dot " + (s.bot.status || "");
  const acc = s.account;
  document.getElementById("botStatus").textContent =
    `bot: ${s.bot.status}` +
    (s.bot.minutes_since != null ? ` (最終チェック ${s.bot.minutes_since} 分前)` : "") +
    (acc && acc.margin_ratio != null ? ` ・ 証拠金維持率 ${acc.margin_ratio.toFixed(1)}%` : "");
  document.getElementById("cacheBackend").textContent = s.cache_backend;
  document.getElementById("generatedAt").textContent = "生成: " + fmtTs(s.generated_at);
}

function lineChart(id, existing, labels, datasets) {
  if (existing) existing.destroy();
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { color: "#8a97b1", maxTicksLimit: 10 }, grid: { color: "#29344f" } },
        y: { ticks: { color: "#8a97b1" }, grid: { color: "#29344f" } },
      },
      plugins: { legend: { labels: { color: "#e7ecf5" } } },
    },
  });
}

async function renderEquity() {
  const rows = await getJSON("/api/equity-curve");
  const labels = rows.map((r) => r.date);
  equityChart = lineChart("equityChart", equityChart, labels, [
    {
      label: "有効証拠金",
      data: rows.map((r) => r.equity_jpy),
      borderColor: "#4c9ffe",
      backgroundColor: "rgba(76,159,254,0.15)",
      fill: true,
      tension: 0.2,
      pointRadius: 0,
    },
    {
      label: "実現損益(累積)",
      data: rows.map((r) => r.cum_realized_pnl_jpy),
      borderColor: "#33c27f",
      pointRadius: 0,
      tension: 0.2,
    },
  ]);
}

async function renderDaily() {
  const rows = await getJSON("/api/daily-pnl");
  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(document.getElementById("dailyPnlChart"), {
    type: "bar",
    data: {
      labels: rows.map((r) => r.date),
      datasets: [
        {
          label: "日次損益",
          data: rows.map((r) => r.pnl_jpy),
          backgroundColor: rows.map((r) =>
            r.pnl_jpy >= 0 ? "rgba(51,194,127,0.8)" : "rgba(242,86,91,0.8)"
          ),
        },
      ],
    },
    options: {
      responsive: true,
      scales: {
        x: { ticks: { color: "#8a97b1", maxTicksLimit: 12 }, grid: { display: false } },
        y: { ticks: { color: "#8a97b1" }, grid: { color: "#29344f" } },
      },
      plugins: { legend: { display: false } },
    },
  });
}

async function renderSignals() {
  const d = await getJSON("/api/signal-stats");
  if (signalChart) signalChart.destroy();
  const rows = d.by_result || [];
  signalChart = new Chart(document.getElementById("signalChart"), {
    type: "doughnut",
    data: {
      labels: rows.map((r) => r.result),
      datasets: [
        {
          data: rows.map((r) => r.count),
          backgroundColor: rows.map((r) =>
            r.result === "EXECUTED"
              ? "#33c27f"
              : r.result === "MARGIN_INSUFFICIENT"
              ? "#f2565b"
              : "#8a97b1"
          ),
        },
      ],
    },
    options: { responsive: true, plugins: { legend: { labels: { color: "#e7ecf5" } } } },
  });
  const note = document.getElementById("signalNote");
  if (d.execution_rate != null) {
    note.textContent =
      `発注成功率 ${(d.execution_rate * 100).toFixed(1)}%` +
      `(発注 ${d.executed + d.margin_insufficient} 件中 ${d.margin_insufficient} 件が証拠金不足で失注)`;
  } else {
    note.textContent = "";
  }
}

async function renderCloudWatch() {
  const d = await getJSON("/api/cloudwatch");
  const el = document.getElementById("cloudwatch");
  if (!d.available) {
    el.innerHTML = `<div class="cw-unavailable">利用不可(${d.error || "認証情報なし"})。AWS 認証情報を設定すると表示されます。</div>`;
    return;
  }
  if (!d.alarms.length) {
    el.innerHTML = `<div class="cw-unavailable">対象アラームが見つかりません。</div>`;
    return;
  }
  el.innerHTML = d.alarms
    .map(
      (a) => `<div class="cw-item">
        <span>${a.name}</span>
        <span class="cw-state ${a.state}">${a.state}</span>
      </div>`
    )
    .join("");
}

function table(id, headers, rows) {
  const thead = "<thead><tr>" + headers.map((h) => `<th>${h}</th>`).join("") + "</tr></thead>";
  const tbody =
    "<tbody>" +
    rows.map((cells) => "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") +
    "</tbody>";
  document.getElementById(id).innerHTML = thead + tbody;
}

async function renderStrategy() {
  const rows = await getJSON("/api/by-strategy");
  table(
    "strategyTable",
    ["手法", "取引数", "勝率", "純損益", "期待値", "PF"],
    rows.map((r) => [
      r.strategy,
      r.trades,
      pct(r.win_rate),
      `<span class="${sign(r.net_pnl_jpy)}">${yen(r.net_pnl_jpy)}</span>`,
      `<span class="${sign(r.expectancy_jpy)}">${yen(r.expectancy_jpy)}</span>`,
      r.profit_factor ?? "-",
    ])
  );
}

async function renderPair() {
  const rows = await getJSON("/api/by-pair");
  table(
    "pairTable",
    ["ペア", "取引数", "勝率", "純損益", "期待値"],
    rows.map((r) => [
      (r.watch ? "★ " : "") + r.pair,
      r.trades,
      pct(r.win_rate),
      `<span class="${sign(r.net_pnl_jpy)}">${yen(r.net_pnl_jpy)}</span>`,
      `<span class="${sign(r.expectancy_jpy)}">${yen(r.expectancy_jpy)}</span>`,
    ])
  );
}

async function renderOpen() {
  const rows = await getJSON("/api/open-positions");
  table(
    "openTable",
    ["ポジションID", "ペア", "手法", "方向", "建玉時刻", "価格", "Lot"],
    rows.length
      ? rows.map((r) => [
          r.position_id,
          r.pair,
          r.strategy,
          r.side,
          fmtTs(r.entry_time),
          r.entry_price,
          r.lot,
        ])
      : [["-", "-", "-", "-", "-", "-", "-"]]
  );
}

async function renderEvents() {
  const rows = await getJSON("/api/recent-events?limit=50");
  const badge = (r) => {
    if (r.event_type === "exit") return `<span class="tag closed">CLOSED</span>`;
    if (r.result === "EXECUTED") return `<span class="tag exec">EXECUTED</span>`;
    if (r.result === "MARGIN_INSUFFICIENT") return `<span class="tag fail">失注</span>`;
    return `<span class="tag">${r.event_type}</span>`;
  };
  table(
    "eventsTable",
    ["時刻", "ペア", "手法", "方向", "種別", "価格", "損益", "理由"],
    rows.map((r) => [
      fmtTs(r.ts),
      r.pair,
      r.strategy,
      r.side ?? "-",
      badge(r),
      r.price ?? "-",
      r.pnl_jpy != null ? `<span class="${sign(r.pnl_jpy)}">${yen(r.pnl_jpy)}</span>` : "-",
      r.reason ?? r.detail ?? "-",
    ])
  );
}

async function refreshAll() {
  try {
    renderKPI(await getJSON("/api/summary"));
    await Promise.all([
      renderReconciliation(),
      renderParity(),
      renderEquity(),
      renderDaily(),
      renderSignals(),
      renderCloudWatch(),
      renderStrategy(),
      renderPair(),
      renderOpen(),
      renderEvents(),
    ]);
  } catch (e) {
    console.error(e);
    document.getElementById("botStatus").textContent = "読み込みエラー: " + e.message;
  }
}

document.getElementById("refreshBtn").addEventListener("click", refreshAll);
refreshAll();
setInterval(refreshAll, 60_000);
