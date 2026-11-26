/* BZU MULTI · Multi-Asset MEXC Scanner + Live Token Chart & Trade Visualization */
(function () {
  'use strict';

  const STATE_URL = './multi_scanner_state.json';
  const MEXC_TICKER_URL = 'https://contract.mexc.com/api/v1/contract/ticker';
  let latestData = null;
  let selectedSymbol = null;
  let selectedTf = '15m';
  let livePrices = {};
  let chart = null;
  let candleSeries = null;
  let priceLines = [];

  const $ = (id) => document.getElementById(id);

  function fmtPrice(v, tick) {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
    const num = Number(v);
    let dec = 2;
    if (tick && tick > 0 && tick < 1) {
      dec = Math.min(6, Math.max(0, Math.round(-Math.log10(tick))));
    } else if (num < 1) {
      dec = 4;
    } else if (num < 20) {
      dec = 3;
    }
    return num.toFixed(dec);
  }

  function arrow(bias) {
    if (bias === 1) return '<span class="ms-up">↑</span>';
    if (bias === -1) return '<span class="ms-dn">↓</span>';
    return '<span class="ms-flat">↔</span>';
  }

  function getRiskInputs() {
    const dep = Math.max(10, parseFloat($('ms-deposit')?.value || '100') || 100);
    const riskPct = Math.max(0.5, Math.min(20, parseFloat($('ms-risk')?.value || '3') || 3));
    const maxLev = Math.max(1, Math.min(50, parseFloat($('ms-lev')?.value || '5') || 5));
    return { dep, riskPct, maxLev };
  }

  function calcSizing(entry, stop, tp) {
    if (!entry || !stop || !tp) return null;
    const { dep, riskPct, maxLev } = getRiskInputs();
    const slPct = Math.abs(entry - stop) / entry;
    const tpPct = Math.abs(tp - entry) / entry;
    if (!Number.isFinite(slPct) || slPct <= 0) return null;
    const targetRisk = dep * (riskPct / 100);
    const maxNotional = dep * maxLev;
    const notional = Math.min(maxNotional, targetRisk / slPct);
    const effLev = notional / dep;
    const riskUsd = notional * slPct;
    const profitUsd = notional * tpPct;
    return {
      notional: notional.toFixed(0),
      effLev: effLev.toFixed(2),
      riskUsd: riskUsd.toFixed(2),
      profitUsd: profitUsd.toFixed(2),
      slPct: (slPct * 100).toFixed(2),
      tpPct: (tpPct * 100).toFixed(2),
    };
  }

  function statusClass(status) {
    if (status === 'READY') return 'ms-pill-ready';
    if (status === 'WATCH') return 'ms-pill-watch';
    return 'ms-pill-wait';
  }

  function clusterClass(cluster) {
    if (cluster === 'ENERGY') return 'ms-cl-energy';
    if (cluster === 'METALS') return 'ms-cl-metals';
    return 'ms-cl-crypto';
  }

  function ensureChart() {
    const container = $('ms-chart');
    if (!container || typeof window.LightweightCharts === 'undefined') return false;
    if (chart) return true;
    chart = window.LightweightCharts.createChart(container, {
      layout: {
        background: { color: '#0d111a' },
        textColor: '#94a3b8',
        fontFamily: 'Inter, system-ui, sans-serif',
      },
      grid: {
        vertLines: { color: 'rgba(148, 163, 184, 0.08)' },
        horzLines: { color: 'rgba(148, 163, 184, 0.08)' },
      },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: 'rgba(148, 163, 184, 0.18)' },
      timeScale: { borderColor: 'rgba(148, 163, 184, 0.18)', timeVisible: true, secondsVisible: false },
      height: 310,
    });
    if (typeof chart.addSeries === 'function' && window.LightweightCharts.CandlestickSeries) {
      candleSeries = chart.addSeries(window.LightweightCharts.CandlestickSeries, {
        upColor: '#22c55e',
        downColor: '#ef4444',
        borderUpColor: '#22c55e',
        borderDownColor: '#ef4444',
        wickUpColor: '#22c55e',
        wickDownColor: '#ef4444',
      });
    } else if (typeof chart.addCandlestickSeries === 'function') {
      candleSeries = chart.addCandlestickSeries({
        upColor: '#22c55e',
        downColor: '#ef4444',
        borderUpColor: '#22c55e',
        borderDownColor: '#ef4444',
        wickUpColor: '#22c55e',
        wickDownColor: '#ef4444',
      });
    }
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => {
        if (chart && container.clientWidth > 0) {
          chart.applyOptions({ width: container.clientWidth });
        }
      }).observe(container);
    }
    return Boolean(candleSeries);
  }

  function clearPriceLines() {
    if (!candleSeries) return;
    for (const pl of priceLines) {
      try { candleSeries.removePriceLine(pl); } catch (_) {}
    }
    priceLines = [];
  }

  function renderChartFor(item) {
    const titleEl = $('ms-chart-title');
    if (titleEl && item) {
      titleEl.textContent = `Графік ${item.label} (${selectedTf.toUpperCase()})`;
    }
    if (!item || !ensureChart()) return;

    const raw = selectedTf === '5m' ? (item.candles_5m || item.candles_15m || []) : (item.candles_15m || item.candles_5m || []);
    const tick = item.tick || 0.01;
    const dec = tick > 0 && tick < 1 ? Math.min(6, Math.max(0, Math.round(-Math.log10(tick)))) : 2;
    candleSeries.applyOptions({
      priceFormat: { type: 'price', precision: dec, minMove: tick },
    });

    if (raw.length) {
      const bars = raw.map((b) => ({
        time: Number(b.t),
        open: Number(b.o),
        high: Number(b.h),
        low: Number(b.l),
        close: Number(b.c),
      }));
      // Update last candle close with live MEXC price if available
      const liveP = livePrices[item.symbol];
      if (Number.isFinite(liveP) && bars.length) {
        const last = bars[bars.length - 1];
        last.close = liveP;
        if (liveP > last.high) last.high = liveP;
        if (liveP < last.low) last.low = liveP;
      }
      candleSeries.setData(bars);
      chart.timeScale().fitContent();
    }

    clearPriceLines();
    const sz = calcSizing(item.entry, item.stop, item.tp);

    if (Number.isFinite(Number(item.entry))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.entry),
        color: '#f97316',
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: item.status === 'READY'
          ? (item.order_type === 'LIMIT' ? `ЛІМІТ ВХІД ${item.side_ua}` : `ВХІД ${item.side_ua}`)
          : `ЗОНА ВХОДУ ${item.side_ua}`,
      }));
    }
    if (Number.isFinite(Number(item.stop))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.stop),
        color: '#ef4444',
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: sz ? `SL (−${sz.slPct}% · −$${sz.riskUsd})` : 'SL',
      }));
    }
    if (Number.isFinite(Number(item.tp))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.tp),
        color: '#22c55e',
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: sz ? `TP (+${sz.tpPct}% · +$${sz.profitUsd})` : 'TP',
      }));
    }
    if (Number.isFinite(Number(item.be_trigger))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.be_trigger),
        color: '#eab308',
        lineWidth: 1,
        lineStyle: 3,
        axisLabelVisible: true,
        title: 'Б/У (+0.90R)',
      }));
    }
  }

  function renderHighlight(item, isTopPick) {
    const card = $('ms-highlight');
    if (!card) return;
    if (!item) {
      card.innerHTML = '<div class="ms-empty">Очікуємо перший цикл BZU MULTI…</div>';
      return;
    }
    const price = livePrices[item.symbol] ?? item.price;
    const sz = calcSizing(item.entry, item.stop, item.tp);
    const sideCls = item.side === 1 ? 'ms-side-long' : item.side === -1 ? 'ms-side-short' : 'ms-side-flat';
    const orderLbl = item.status === 'READY'
      ? (item.order_type === 'LIMIT' ? 'ЛІМІТКА (0% Maker)' : 'РИНКОВИЙ ВХІД (M5)')
      : 'ЩЕ НЕ ВХОДИМО (ОЧІКУВАННЯ)';
    const badgeText = isTopPick ? '★ №1 НАЙКРАЩИЙ ВИБІР' : 'ВИБРАНИЙ АКТИВ';

    card.innerHTML = `
      <div class="ms-hl-top">
        <div class="ms-hl-badges">
          <span class="ms-top-tag">${badgeText}</span>
          <span class="ms-cl ${clusterClass(item.cluster)}">${item.cluster_ua}</span>
          <span class="ms-fee">${item.fee_tag || '0% Maker'}</span>
        </div>
        <span class="ms-pill ${statusClass(item.status)}">${item.status_ua} · ${item.score}/100</span>
      </div>
      <div class="ms-hl-title-row">
        <div>
          <h3 class="ms-hl-sym">${item.label} <span class="ms-hl-price">${fmtPrice(price, item.tick)} $</span></h3>
          <p class="ms-hl-sub">${item.setup_ua || 'Аналіз структури'} · <strong class="${sideCls}">${item.side_ua}</strong> · ${orderLbl}</p>
        </div>
        <a class="ms-mexc-btn" href="${item.mexc_url || 'https://www.mexc.com/futures/' + item.symbol}" target="_blank" rel="noopener">Відкрити на MEXC ↗</a>
      </div>
      <div class="ms-hl-levels">
        <div class="ms-lvl"><small>ВХІД</small><strong>${fmtPrice(item.entry, item.tick)}</strong></div>
        <div class="ms-lvl"><small>СТОП-ЛОСС</small><strong class="ms-dn">${fmtPrice(item.stop, item.tick)}${sz ? ' (−' + sz.slPct + '%)' : ''}</strong></div>
        <div class="ms-lvl"><small>ТЕЙК-ПРОФІТ</small><strong class="ms-up">${fmtPrice(item.tp, item.tick)}${sz ? ' (+' + sz.tpPct + '%)' : ''}</strong></div>
        <div class="ms-lvl"><small>R:R / RVOL</small><strong>${item.rr ? item.rr.toFixed(2) + ' R' : '—'} · ${item.rvol ? item.rvol.toFixed(2) + '×' : '—'}</strong></div>
      </div>
      ${sz ? `
      <div class="ms-sizing-bar">
        <div><small>ПОЗИЦІЯ (USDT)</small><strong>$${sz.notional}</strong></div>
        <div><small>ПЛЕЧЕ</small><strong>${sz.effLev}×</strong></div>
        <div><small>РИЗИК НА SL</small><strong class="ms-dn">−$${sz.riskUsd}</strong></div>
        <div><small>ПРОФІТ НА TP</small><strong class="ms-up">+$${sz.profitUsd}</strong></div>
      </div>` : ''}
      <p class="ms-hl-reason">${item.reason_ua || ''}</p>
    `;
  }

  function renderTable(data) {
    const tbody = $('ms-rows');
    if (!tbody) return;
    const list = data.instruments || [];
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="ms-empty">Дані BZU MULTI ще не згенеровані. Запусти цикл GitHub Actions.</td></tr>';
      return;
    }

    const topSym = data.top_pick ? data.top_pick.symbol : list[0].symbol;
    if (!selectedSymbol || !list.some((x) => x.symbol === selectedSymbol)) {
      selectedSymbol = topSym;
    }

    tbody.innerHTML = list.map((item) => {
      const price = livePrices[item.symbol] ?? item.price;
      const chg = Number(item.change_24h_pct || 0);
      const chgCls = chg > 0 ? 'ms-up' : chg < 0 ? 'ms-dn' : 'ms-flat';
      const activeCls = item.symbol === selectedSymbol ? 'ms-row-selected' : '';
      const isTop = item.symbol === topSym;
      return `
        <tr class="ms-row ${activeCls}" data-symbol="${item.symbol}">
          <td>
            <div class="ms-sym-cell">
              <strong>${isTop ? '★ ' : ''}${item.short}</strong>
              <span class="ms-cl ${clusterClass(item.cluster)}">${item.cluster_ua}</span>
            </div>
          </td>
          <td class="ms-num">
            <div>${fmtPrice(price, item.tick)}</div>
            <small class="${chgCls}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</small>
          </td>
          <td class="ms-tf">${arrow(item.bias_4h)} ${arrow(item.bias_1h)} ${arrow(item.bias_15m)}</td>
          <td><span class="ms-pill ${statusClass(item.status)}">${item.status_ua}</span></td>
          <td class="ms-num ${item.side === 1 ? 'ms-up' : item.side === -1 ? 'ms-dn' : ''}">${item.side === 1 ? 'LONG' : item.side === -1 ? 'SHORT' : '—'}</td>
          <td class="ms-num">${item.rr ? item.rr.toFixed(2) : '—'}</td>
          <td class="ms-num"><strong>${item.score}</strong></td>
        </tr>
      `;
    }).join('');

    tbody.querySelectorAll('tr.ms-row').forEach((tr) => {
      tr.addEventListener('click', () => {
        selectedSymbol = tr.getAttribute('data-symbol');
        renderAll();
      });
    });

    const chosen = list.find((x) => x.symbol === selectedSymbol) || list[0];
    renderHighlight(chosen, chosen && chosen.symbol === topSym);
    renderChartFor(chosen);
  }

  function renderMeta(data) {
    const btcEl = $('ms-btc-pill');
    if (btcEl && data.btc_macro) {
      const b = data.btc_macro;
      const cls = b.side === 1 ? 'ms-up' : b.side === -1 ? 'ms-dn' : 'ms-flat';
      const p = livePrices.BTC_USDT ?? b.price;
      btcEl.innerHTML = `BTC Макро: <strong class="${cls}">${b.label_ua}</strong> · $${p ? Number(p).toLocaleString('en-US', { maximumFractionDigits: 0 }) : '—'}`;
    }
    const updEl = $('ms-updated');
    if (updEl) {
      updEl.textContent = data.updated_kyiv ? `Оновлено: ${data.updated_kyiv} Київ · Активних входів: ${data.ready_count || 0}` : 'Очікуємо зліпок MEXC';
    }
  }

  function renderAll() {
    if (!latestData) return;
    renderMeta(latestData);
    renderTable(latestData);
  }

  async function loadScannerState() {
    try {
      const res = await fetch(`${STATE_URL}?t=${Date.now()}`, { cache: 'no-store' });
      if (!res.ok) return;
      latestData = await res.json();
      renderAll();
    } catch (_) {}
  }

  async function refreshLiveMexcPrices() {
    try {
      const res = await fetch(MEXC_TICKER_URL, { cache: 'no-store' });
      if (!res.ok) return;
      const json = await res.json();
      if (!json || !Array.isArray(json.data)) return;
      const wanted = new Set(['UKOIL_USDT', 'XPT_USDT', 'SOL_USDT', 'LINK_USDT', 'LTC_USDT', 'APT_USDT', 'ORDI_USDT', 'BTC_USDT']);
      for (const row of json.data) {
        if (wanted.has(row.symbol) && Number.isFinite(Number(row.lastPrice))) {
          livePrices[row.symbol] = Number(row.lastPrice);
        }
      }
      renderAll();
    } catch (_) {}
  }

  function init() {
    if (!$('multi-scanner-section')) return;
    ['ms-deposit', 'ms-risk', 'ms-lev'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('input', renderAll);
    });
    const b5 = $('ms-tf-5m');
    const b15 = $('ms-tf-15m');
    if (b5 && b15) {
      b5.addEventListener('click', () => {
        selectedTf = '5m';
        b5.setAttribute('aria-pressed', 'true');
        b15.setAttribute('aria-pressed', 'false');
        renderAll();
      });
      b15.addEventListener('click', () => {
        selectedTf = '15m';
        b15.setAttribute('aria-pressed', 'true');
        b5.setAttribute('aria-pressed', 'false');
        renderAll();
      });
    }
    loadScannerState();
    refreshLiveMexcPrices();
    setInterval(loadScannerState, 15000);
    setInterval(refreshLiveMexcPrices, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
