/* BZU MULTI · Compact Left-Column Chart (TradingView + Fixed BZU Levels) & 7-Asset MEXC Scanner */
(function () {
  'use strict';

  const STATE_URL = './multi_scanner_state.json';
  const MEXC_TICKER_URL = 'https://contract.mexc.com/api/v1/contract/ticker';
  const TV_MAP = {
    UKOIL_USDT: 'TVC:UKOIL',
    XPT_USDT: 'TVC:PLATINUM',
    SOL_USDT: 'MEXC:SOLUSDT.P',
    LINK_USDT: 'MEXC:LINKUSDT.P',
    LTC_USDT: 'MEXC:LTCUSDT.P',
    APT_USDT: 'MEXC:APTUSDT.P',
    ORDI_USDT: 'MEXC:ORDIUSDT.P',
  };

  let latestData = null;
  let selectedSymbol = null;
  let selectedTf = '15m';
  let chartMode = 'tv'; // 'tv' (TradingView default) | 'bzu' (LightweightCharts with Entry/SL/TP/BE+)
  let livePrices = {};
  let tvRenderedKey = '';
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

  function destroyLwc() {
    if (chart) {
      try { chart.remove(); } catch (_) {}
      chart = null;
      candleSeries = null;
      priceLines = [];
    }
  }

  function renderTradingView(item) {
    const tvBox = $('ms-chart-tv');
    const bzuBox = $('ms-chart');
    const legend = $('ms-chart-legend');
    if (!tvBox || !bzuBox) return;
    bzuBox.hidden = true;
    tvBox.hidden = false;
    if (legend) legend.hidden = true;
    destroyLwc();

    const tvSym = TV_MAP[item.symbol] || `MEXC:${item.symbol.replace('_', '')}.P`;
    const tvInterval = selectedTf === '5m' ? '5' : '15';
    const key = `${tvSym}:${tvInterval}`;
    if (tvRenderedKey === key && tvBox.firstChild) return;
    tvRenderedKey = key;

    const src = 'https://s.tradingview.com/widgetembed/?' + new URLSearchParams({
      symbol: tvSym,
      interval: tvInterval,
      theme: 'dark',
      style: '1',
      locale: 'uk',
      timezone: 'Europe/Kyiv',
      hide_top_toolbar: '0',
      hide_legend: '1',
      save_image: '0',
      allow_symbol_change: '0',
      withdateranges: '0',
    }).toString();

    tvBox.innerHTML = `<iframe class="ms-tv-iframe" src="${src}" title="TradingView ${item.label}" loading="lazy" allowtransparency="true" frameborder="0"></iframe>`;
  }

  function renderBzuLevelsChart(item) {
    const tvBox = $('ms-chart-tv');
    const bzuBox = $('ms-chart');
    const legend = $('ms-chart-legend');
    if (!tvBox || !bzuBox) return;
    tvBox.hidden = true;
    bzuBox.hidden = false;
    if (legend) legend.hidden = false;

    if (typeof window.LightweightCharts === 'undefined') return;
    // Recreate chart cleanly on symbol switch so right priceScale NEVER keeps stale scale from another token!
    destroyLwc();
    bzuBox.innerHTML = '';

    const raw = selectedTf === '5m' ? (item.candles_5m || item.candles_15m || []) : (item.candles_15m || item.candles_5m || []);
    const bars = raw
      .map((b) => ({
        time: Number(b.t),
        open: Number(b.o),
        high: Number(b.h),
        low: Number(b.l),
        close: Number(b.c),
      }))
      .filter((b) => Number.isFinite(b.time) && b.open > 0 && b.high >= b.low);

    if (!bars.length) {
      bzuBox.innerHTML = '<div class="ms-empty">Очікуємо свічки після наступного циклу…</div>';
      return;
    }

    const liveP = livePrices[item.symbol];
    if (Number.isFinite(liveP) && liveP > 0) {
      const last = bars[bars.length - 1];
      last.close = liveP;
      if (liveP > last.high) last.high = liveP;
      if (liveP < last.low) last.low = liveP;
    }

    const tick = item.tick || 0.01;
    const dec = tick > 0 && tick < 1 ? Math.min(6, Math.max(0, Math.round(-Math.log10(tick)))) : 2;
    const width = Math.max(280, bzuBox.clientWidth || 480);

    chart = window.LightweightCharts.createChart(bzuBox, {
      width,
      height: 270,
      layout: { background: { color: '#0d111a' }, textColor: '#94a3b8' },
      grid: {
        vertLines: { color: 'rgba(148, 163, 184, 0.08)' },
        horzLines: { color: 'rgba(148, 163, 184, 0.08)' },
      },
      rightPriceScale: { autoScale: true, borderColor: 'rgba(148, 163, 184, 0.18)', scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: 'rgba(148, 163, 184, 0.18)', timeVisible: true, secondsVisible: false },
    });

    const opts = {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      priceFormat: { type: 'price', precision: dec, minMove: tick },
    };
    candleSeries = (typeof chart.addSeries === 'function' && window.LightweightCharts.CandlestickSeries)
      ? chart.addSeries(window.LightweightCharts.CandlestickSeries, opts)
      : chart.addCandlestickSeries(opts);

    candleSeries.setData(bars);
    const sz = calcSizing(item.entry, item.stop, item.tp);

    if (Number.isFinite(Number(item.entry))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.entry), color: '#f97316', lineWidth: 2, lineStyle: 0, axisLabelVisible: true,
        title: item.status === 'READY' ? `ВХІД ${item.side_ua}` : `ЗОНА ${item.side_ua}`,
      }));
    }
    if (Number.isFinite(Number(item.stop))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.stop), color: '#ef4444', lineWidth: 2, lineStyle: 2, axisLabelVisible: true,
        title: sz ? `SL −${sz.slPct}%` : 'SL',
      }));
    }
    if (Number.isFinite(Number(item.tp))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.tp), color: '#22c55e', lineWidth: 2, lineStyle: 0, axisLabelVisible: true,
        title: sz ? `TP +${sz.tpPct}%` : 'TP',
      }));
    }
    if (Number.isFinite(Number(item.be_trigger))) {
      priceLines.push(candleSeries.createPriceLine({
        price: Number(item.be_trigger), color: '#eab308', lineWidth: 1, lineStyle: 3, axisLabelVisible: true,
        title: 'Б/У',
      }));
    }
    chart.timeScale().fitContent();
  }

  function renderChartFor(item) {
    const titleEl = $('ms-chart-title');
    if (titleEl && item) {
      titleEl.textContent = `${item.label} · ${selectedTf.toUpperCase()}`;
    }
    if (!item) return;
    if (chartMode === 'tv') {
      renderTradingView(item);
    } else {
      renderBzuLevelsChart(item);
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
        <a class="ms-mexc-btn" href="${item.mexc_url || 'https://www.mexc.com/futures/' + item.symbol}" target="_blank" rel="noopener">MEXC ↗</a>
      </div>
      <div class="ms-hl-levels">
        <div class="ms-lvl"><small>ВХІД</small><strong>${fmtPrice(item.entry, item.tick)}</strong></div>
        <div class="ms-lvl"><small>СТОП-ЛОСС</small><strong class="ms-dn">${fmtPrice(item.stop, item.tick)}${sz ? ' (−' + sz.slPct + '%)' : ''}</strong></div>
        <div class="ms-lvl"><small>ТЕЙК-ПРОФІТ</small><strong class="ms-up">${fmtPrice(item.tp, item.tick)}${sz ? ' (+' + sz.tpPct + '%)' : ''}</strong></div>
        <div class="ms-lvl"><small>R:R / RVOL</small><strong>${item.rr ? item.rr.toFixed(2) + ' R' : '—'} · ${item.rvol ? item.rvol.toFixed(2) + '×' : '—'}</strong></div>
      </div>
      ${sz ? `
      <div class="ms-sizing-bar">
        <div><small>ПОЗИЦІЯ</small><strong>$${sz.notional}</strong></div>
        <div><small>ПЛЕЧЕ</small><strong>${sz.effLev}×</strong></div>
        <div><small>РИЗИК SL</small><strong class="ms-dn">−$${sz.riskUsd}</strong></div>
        <div><small>ПРОФІТ TP</small><strong class="ms-up">+$${sz.profitUsd}</strong></div>
      </div>` : ''}
      <p class="ms-hl-reason">${item.reason_ua || ''}</p>
    `;
  }

  function renderTable(data, updateChart) {
    const tbody = $('ms-rows');
    if (!tbody) return;
    const list = data.instruments || [];
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="ms-empty">Дані BZU MULTI ще не згенеровані.</td></tr>';
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
        renderAll(true);
      });
    });

    const chosen = list.find((x) => x.symbol === selectedSymbol) || list[0];
    renderHighlight(chosen, chosen && chosen.symbol === topSym);
    if (updateChart) renderChartFor(chosen);
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

  function renderAll(updateChart = false) {
    if (!latestData) return;
    renderMeta(latestData);
    renderTable(latestData, updateChart);
  }

  async function loadScannerState() {
    try {
      const res = await fetch(`${STATE_URL}?t=${Date.now()}`, { cache: 'no-store' });
      if (!res.ok) return;
      const first = !latestData;
      latestData = await res.json();
      renderAll(first || chartMode === 'bzu');
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
      renderAll(false);
    } catch (_) {}
  }

  function init() {
    if (!$('multi-scanner-section')) return;
    ['ms-deposit', 'ms-risk', 'ms-lev'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('input', () => renderAll(chartMode === 'bzu'));
    });

    const b5 = $('ms-tf-5m'), b15 = $('ms-tf-15m');
    if (b5 && b15) {
      b5.addEventListener('click', () => {
        selectedTf = '5m'; b5.setAttribute('aria-pressed', 'true'); b15.setAttribute('aria-pressed', 'false');
        renderAll(true);
      });
      b15.addEventListener('click', () => {
        selectedTf = '15m'; b15.setAttribute('aria-pressed', 'true'); b5.setAttribute('aria-pressed', 'false');
        renderAll(true);
      });
    }

    const mTv = $('ms-mode-tv'), mBzu = $('ms-mode-bzu');
    if (mTv && mBzu) {
      mTv.addEventListener('click', () => {
        chartMode = 'tv'; mTv.setAttribute('aria-pressed', 'true'); mBzu.setAttribute('aria-pressed', 'false');
        tvRenderedKey = '';
        renderAll(true);
      });
      mBzu.addEventListener('click', () => {
        chartMode = 'bzu'; mBzu.setAttribute('aria-pressed', 'true'); mTv.setAttribute('aria-pressed', 'false');
        renderAll(true);
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
