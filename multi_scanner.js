/* BZU MULTI · Live WebSocket MEXC Prices + Clean TradingView / Entry-Levels Chart */
(function () {
  'use strict';

  const STATE_URL = './multi_scanner_state.json';
  const LIVE_STATE_URL = './live_state.json';
  const MEXC_WS_URL = 'wss://contract.mexc.com/edge';
  const SYMBOLS = ['UKOIL_USDT', 'XPT_USDT', 'SOL_USDT', 'LINK_USDT', 'LTC_USDT', 'APT_USDT', 'ORDI_USDT', 'BTC_USDT'];
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
  let liveOilState = null;
  let selectedSymbol = null;
  let selectedTf = '15m';
  let chartMode = 'tv'; // 'tv' | 'levels'
  let livePrices = {};
  let liveChanges = {};
  let tvRenderedKey = '';
  let chart = null;
  let candleSeries = null;
  let priceLines = [];
  let lastBars = [];
  let ws = null;
  let wsPingTimer = null;

  const $ = (id) => document.getElementById(id);

  function posNum(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  function fmtPrice(v, tick) {
    const num = posNum(v);
    if (num === null) return '—';
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
    const e = posNum(entry), s = posNum(stop), t = posNum(tp);
    if (!e || !s || !t) return null;
    const { dep, riskPct, maxLev } = getRiskInputs();
    const slPct = Math.abs(e - s) / e;
    const tpPct = Math.abs(t - e) / e;
    if (!Number.isFinite(slPct) || slPct <= 0) return null;
    const targetRisk = dep * (riskPct / 100);
    const maxNotional = dep * maxLev;
    const notional = Math.min(maxNotional, targetRisk / slPct);
    const effLev = notional / dep;
    return {
      notional: notional.toFixed(0),
      effLev: effLev.toFixed(2),
      riskUsd: (notional * slPct).toFixed(2),
      profitUsd: (notional * tpPct).toFixed(2),
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
      lastBars = [];
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

  function renderLevelsChart(item) {
    const tvBox = $('ms-chart-tv');
    const bzuBox = $('ms-chart');
    const legend = $('ms-chart-legend');
    if (!tvBox || !bzuBox) return;
    tvBox.hidden = true;
    bzuBox.hidden = false;
    if (legend) legend.hidden = false;

    if (typeof window.LightweightCharts === 'undefined') return;
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

    const liveP = posNum(livePrices[item.symbol]);
    if (liveP !== null) {
      const last = bars[bars.length - 1];
      last.close = liveP;
      if (liveP > last.high) last.high = liveP;
      if (liveP < last.low) last.low = liveP;
    }
    lastBars = bars;

    const tick = item.tick || 0.01;
    const dec = tick > 0 && tick < 1 ? Math.min(6, Math.max(0, Math.round(-Math.log10(tick)))) : 2;
    const width = Math.max(280, bzuBox.clientWidth || 480);

    chart = window.LightweightCharts.createChart(bzuBox, {
      width,
      height: 270,
      layout: { background: { color: '#0d111a' }, textColor: '#94a3b8' },
      grid: {
        vertLines: { color: 'rgba(148, 163, 184, 0.06)' },
        horzLines: { color: 'rgba(148, 163, 184, 0.06)' },
      },
      rightPriceScale: { autoScale: true, borderColor: 'rgba(148, 163, 184, 0.16)', scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: 'rgba(148, 163, 184, 0.16)', timeVisible: true, secondsVisible: false },
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

    // Draw thin, clean 1px lines ONLY when real positive entry/SL/TP exist (never 0.00!)
    const entryP = posNum(item.entry);
    const stopP = posNum(item.stop);
    const tpP = posNum(item.tp);
    const beP = posNum(item.be_trigger);
    const sz = calcSizing(entryP, stopP, tpP);

    if (entryP !== null) {
      priceLines.push(candleSeries.createPriceLine({
        price: entryP, color: 'rgba(249, 115, 22, 0.55)', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '',
      }));
    }
    if (stopP !== null) {
      priceLines.push(candleSeries.createPriceLine({
        price: stopP, color: 'rgba(239, 68, 68, 0.55)', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '',
      }));
    }
    if (tpP !== null) {
      priceLines.push(candleSeries.createPriceLine({
        price: tpP, color: 'rgba(34, 197, 94, 0.55)', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '',
      }));
    }
    if (beP !== null) {
      priceLines.push(candleSeries.createPriceLine({
        price: beP, color: 'rgba(234, 179, 8, 0.45)', lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: '',
      }));
    }
    chart.timeScale().fitContent();
  }

  function tickChartPrice(sym, price) {
    if (chartMode !== 'levels' || !candleSeries || sym !== selectedSymbol || !lastBars.length) return;
    const last = lastBars[lastBars.length - 1];
    const updated = {
      time: last.time,
      open: last.open,
      high: Math.max(last.high, price),
      low: Math.min(last.low, price),
      close: price,
    };
    lastBars[lastBars.length - 1] = updated;
    try { candleSeries.update(updated); } catch (_) {}
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
      renderLevelsChart(item);
    }
  }

  function renderHighlight(item, isTopPick) {
    const card = $('ms-highlight');
    if (!card) return;
    if (!item) {
      card.innerHTML = '<div class="ms-empty">Очікуємо перший цикл BZU MULTI…</div>';
      return;
    }
    const price = posNum(livePrices[item.symbol]) ?? item.price;
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
          <h3 class="ms-hl-sym">${item.label} <span class="ms-hl-price" id="ms-hl-live-price">${fmtPrice(price, item.tick)} $</span></h3>
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
      const price = posNum(livePrices[item.symbol]) ?? item.price;
      const chg = liveChanges[item.symbol] !== undefined ? liveChanges[item.symbol] : Number(item.change_24h_pct || 0);
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
            <div data-live-price="${item.symbol}">${fmtPrice(price, item.tick)}</div>
            <small class="${chgCls}" data-live-chg="${item.symbol}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</small>
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
      const p = posNum(livePrices.BTC_USDT) ?? b.price;
      btcEl.innerHTML = `BTC Макро: <strong class="${cls}">${b.label_ua}</strong> · $${p ? Number(p).toLocaleString('en-US', { maximumFractionDigits: 0 }) : '—'}`;
    }
    const updEl = $('ms-updated');
    if (updEl) {
      const liveTag = ws && ws.readyState === 1 ? ' · Live WS' : '';
      updEl.textContent = data.updated_kyiv ? `Оновлено: ${data.updated_kyiv} Київ${liveTag} · Активних входів: ${data.ready_count || 0}` : 'Очікуємо зліпок MEXC';
    }
  }

  function patchLivePriceDom(sym, price, chgPct) {
    if (!latestData) return;
    const item = (latestData.instruments || []).find((x) => x.symbol === sym);
    if (sym === 'BTC_USDT') {
      renderMeta(latestData);
      return;
    }
    if (!item) return;
    const cell = document.querySelector(`[data-live-price="${sym}"]`);
    if (cell) {
      const prevTxt = cell.textContent;
      const nextTxt = fmtPrice(price, item.tick);
      if (prevTxt !== nextTxt) {
        const prevVal = parseFloat(prevTxt);
        cell.textContent = nextTxt;
        if (Number.isFinite(prevVal)) {
          const cls = price > prevVal ? 'ms-flash-up' : 'ms-flash-dn';
          cell.classList.remove('ms-flash-up', 'ms-flash-dn');
          void cell.offsetWidth;
          cell.classList.add(cls);
        }
      }
    }
    if (chgPct !== undefined) {
      const chgEl = document.querySelector(`[data-live-chg="${sym}"]`);
      if (chgEl) {
        chgEl.textContent = `${chgPct >= 0 ? '+' : ''}${chgPct.toFixed(2)}%`;
        chgEl.className = chgPct > 0 ? 'ms-up' : chgPct < 0 ? 'ms-dn' : 'ms-flat';
      }
    }
    if (sym === selectedSymbol) {
      const hl = $('ms-hl-live-price');
      if (hl) hl.textContent = `${fmtPrice(price, item.tick)} $`;
      tickChartPrice(sym, price);
    }
    renderDualSlotsAndDaily();
  }

  function connectMexcWebSocket() {
    if (typeof window.WebSocket === 'undefined') return;
    try {
      ws = new WebSocket(MEXC_WS_URL);
      ws.onopen = () => {
        SYMBOLS.forEach((sym) => {
          ws.send(JSON.stringify({ method: 'sub.ticker', param: { symbol: sym } }));
        });
        if (latestData) renderMeta(latestData);
        if (wsPingTimer) clearInterval(wsPingTimer);
        wsPingTimer = setInterval(() => {
          if (ws && ws.readyState === 1) ws.send(JSON.stringify({ method: 'ping' }));
        }, 15000);
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.channel === 'push.ticker' && msg.data && msg.symbol) {
            const p = posNum(msg.data.lastPrice);
            if (p !== null) {
              livePrices[msg.symbol] = p;
              const rate = Number(msg.data.riseFallRate);
              if (Number.isFinite(rate)) liveChanges[msg.symbol] = rate * 100;
              patchLivePriceDom(msg.symbol, p, Number.isFinite(rate) ? rate * 100 : undefined);
            }
          }
        } catch (_) {}
      };
      ws.onclose = () => {
        if (wsPingTimer) clearInterval(wsPingTimer);
        setTimeout(connectMexcWebSocket, 5000);
      };
      ws.onerror = () => {
        try { ws.close(); } catch (_) {}
      };
    } catch (_) {}
  }


  function fmtSigned(v, dec = 2) {
    const n = Number(v || 0);
    return `${n >= 0 ? '+' : ''}${n.toFixed(dec)}`;
  }

  function renderDualSlotsAndDaily() {
    // --- SLOT 1: OIL (from live_state.json) ---
    const oilCard = $('ms-slot-oil');
    const oilBadge = $('ms-slot-oil-badge');
    const oilTitle = $('ms-slot-oil-title');
    const oilSub = $('ms-slot-oil-sub');
    const oilOrders = (liveOilState && (liveOilState.orders || liveOilState.active_orders)) || [];
    const oilActive = oilOrders.find((o) => o && (o.status === 'ACTIVE' || o.status === 'OPEN' || o.filled_ts)) || oilOrders[0] || null;
    const oilDaily = (liveOilState && liveOilState.daily) || {};
    const oilDayTrades = Number(oilDaily.entries || oilDaily.trades || 0);
    const oilDayR = Number(oilDaily.net_r || 0);

    if (oilCard && oilTitle && oilSub && oilBadge) {
      oilCard.classList.remove('ms-slot-pos', 'ms-slot-neg', 'ms-slot-free');
      if (oilActive && posNum(oilActive.entry)) {
        const side = oilActive.side === 1 || oilActive.side === 'LONG' ? 1 : -1;
        const sideTxt = side === 1 ? 'LONG' : 'SHORT';
        const entry = Number(oilActive.entry);
        const stop = posNum(oilActive.stop);
        const tp = posNum(oilActive.tp || oilActive.tp1);
        const risk = posNum(oilActive.risk) || (stop ? Math.abs(entry - stop) : 0);
        const curP = posNum(livePrices.UKOIL_USDT) || posNum(liveOilState && liveOilState.price) || entry;
        const pnlR = risk > 0 ? side * (curP - entry) / risk : 0;
        oilCard.classList.add(pnlR >= 0 ? 'ms-slot-pos' : 'ms-slot-neg');
        oilBadge.textContent = `В УГОДІ · ${fmtSigned(pnlR)} R`;
        oilTitle.textContent = `В УГОДІ ${sideTxt} @ ${entry.toFixed(2)} $`;
        oilSub.textContent = `Ціна ${curP.toFixed(2)} · SL ${stop ? stop.toFixed(2) : '—'} · TP ${tp ? tp.toFixed(2) : '—'} · клік → графік`;
      } else {
        oilCard.classList.add('ms-slot-free');
        oilBadge.textContent = 'ВІЛЬНО';
        oilTitle.textContent = 'Немає відкритої угоди по нафті';
        oilSub.textContent = 'Слот 1 вільний · очікуємо підтверджений сигнал Brent';
      }
    }

    // --- SLOT 2: CRYPTO / METAL (from multi_scanner_state.json -> cm_slot) ---
    const cmCard = $('ms-slot-cm');
    const cmBadge = $('ms-slot-cm-badge');
    const cmTitle = $('ms-slot-cm-title');
    const cmSub = $('ms-slot-cm-sub');
    const cmSlot = (latestData && latestData.cm_slot) || {};
    const cmActive = cmSlot.active || null;
    const cmDaily = cmSlot.daily || { trades: 0, net_r: 0, net_usd: 0 };

    if (cmCard && cmTitle && cmSub && cmBadge) {
      cmCard.classList.remove('ms-slot-pos', 'ms-slot-neg', 'ms-slot-free');
      if (cmActive && posNum(cmActive.entry)) {
        const side = Number(cmActive.side || 1);
        const sideTxt = side === 1 ? 'LONG' : 'SHORT';
        const tick = cmActive.tick || 0.001;
        const entry = Number(cmActive.entry);
        const stop = posNum(cmActive.stop);
        const tp = posNum(cmActive.tp);
        const risk = posNum(cmActive.risk) || (stop ? Math.abs(entry - stop) : 0);
        const curP = posNum(livePrices[cmActive.symbol]) || posNum(cmActive.current_price) || entry;
        const pnlR = risk > 0 ? side * (curP - entry) / risk : Number(cmActive.pnl_r || 0);
        const riskUsd = Number((cmActive.sizing && cmActive.sizing.risk_usd) || 3);
        const pnlUsd = pnlR * riskUsd;
        cmCard.classList.add(pnlR >= 0 ? 'ms-slot-pos' : 'ms-slot-neg');
        cmBadge.textContent = `В УГОДІ · ${fmtSigned(pnlR)} R (${fmtSigned(pnlUsd)}$)`;
        cmTitle.textContent = `${cmActive.label} · ${sideTxt} @ ${fmtPrice(entry, tick)} $`;
        cmSub.textContent = `Ціна ${fmtPrice(curP, tick)} · SL ${fmtPrice(stop, tick)} · TP ${fmtPrice(tp, tick)}${cmActive.be_armed ? ' · Б/У' : ''}`;
      } else {
        cmCard.classList.add('ms-slot-free');
        cmBadge.textContent = 'ВІЛЬНО';
        cmTitle.textContent = 'Немає відкритої угоди по крипті / металу';
        cmSub.textContent = 'Слот 2 вільний · скануємо SOL · LINK · LTC · APT · ORDI · XPT';
      }
    }

    // --- DAILY RESULTS SUMMARY BAR ---
    const dOil = $('ms-day-oil');
    const dCm = $('ms-day-cm');
    const dTot = $('ms-day-total');
    if (dOil) {
      dOil.textContent = `${oilDayTrades} уг. · ${fmtSigned(oilDayR)} R`;
      dOil.className = oilDayR > 0 ? 'ms-up' : oilDayR < 0 ? 'ms-dn' : '';
    }
    const cmTrades = Number(cmDaily.trades || 0);
    const cmR = Number(cmDaily.net_r || 0);
    const cmUsd = Number(cmDaily.net_usd || 0);
    if (dCm) {
      dCm.textContent = `${cmTrades} уг. · ${fmtSigned(cmR)} R (${fmtSigned(cmUsd)}$)`;
      dCm.className = cmR > 0 ? 'ms-up' : cmR < 0 ? 'ms-dn' : '';
    }
    if (dTot) {
      const totTrades = oilDayTrades + cmTrades;
      const totR = oilDayR + cmR;
      dTot.textContent = `${totTrades} уг. · ${fmtSigned(totR)} R`;
      dTot.className = totR > 0 ? 'ms-up' : totR < 0 ? 'ms-dn' : '';
    }
  }

  function renderAll(updateChart = false) {
    if (!latestData) return;
    renderDualSlotsAndDaily();
    renderMeta(latestData);
    renderTable(latestData, updateChart);
  }

  async function loadScannerState() {
    try {
      const [r1, r2] = await Promise.all([
        fetch(`${STATE_URL}?t=${Date.now()}`, { cache: 'no-store' }).catch(() => null),
        fetch(`${LIVE_STATE_URL}?t=${Date.now()}`, { cache: 'no-store' }).catch(() => null),
      ]);
      if (r2 && r2.ok) {
        liveOilState = await r2.json();
      }
      if (r1 && r1.ok) {
        const first = !latestData;
        latestData = await r1.json();
        renderAll(first || chartMode === 'levels');
      } else {
        renderDualSlotsAndDaily();
      }
    } catch (_) {}
  }

  function wireBannerClick() {
    const oilCard = $('ms-slot-oil');
    if (oilCard) {
      oilCard.addEventListener('click', () => {
        selectedSymbol = 'UKOIL_USDT';
        renderAll(true);
        const sec = $('multi-scanner-section');
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
    const cmCard = $('ms-slot-cm');
    if (cmCard) {
      cmCard.addEventListener('click', () => {
        const act = latestData && latestData.cm_slot && latestData.cm_slot.active;
        if (act && act.symbol) {
          selectedSymbol = act.symbol;
          renderAll(true);
        }
        const sec = $('multi-scanner-section');
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }

  function init() {
    wireBannerClick();
    if (!$('multi-scanner-section')) return;
    ['ms-deposit', 'ms-risk', 'ms-lev'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('input', () => renderAll(chartMode === 'levels'));
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

    const mTv = $('ms-mode-tv'), mLvl = $('ms-mode-bzu');
    if (mTv && mLvl) {
      mTv.addEventListener('click', () => {
        chartMode = 'tv'; mTv.setAttribute('aria-pressed', 'true'); mLvl.setAttribute('aria-pressed', 'false');
        tvRenderedKey = '';
        renderAll(true);
      });
      mLvl.addEventListener('click', () => {
        chartMode = 'levels'; mLvl.setAttribute('aria-pressed', 'true'); mTv.setAttribute('aria-pressed', 'false');
        renderAll(true);
      });
    }

    loadScannerState();
    connectMexcWebSocket();
    setInterval(loadScannerState, 15000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
