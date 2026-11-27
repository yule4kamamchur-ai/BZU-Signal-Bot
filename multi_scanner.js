/* BZU MULTI · Live WebSocket MEXC Prices + Clean TradingView / Entry-Levels Chart */
(function () {
  'use strict';

  const STATE_URL = './multi_scanner_state.json';
  // Injected only into the GitHub Pages artifact. Worker stays same-origin.
  const RAW_STATE_URL = document.querySelector('meta[name="bzu-state-source"]')?.content || '';
  let snapshotSource = '';
  let fetchDetails = '';
  const snapshotClock = d => Math.max(Number(d?.updated_ts || 0), Number(d?.last_attempt_ts || 0));
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
  let scannerFetchError = false;
  let scanLoading = false;
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

  function displayedSizing(item) {
    if (item.status === 'IN_TRADE' && item.sizing) {
      const a=item.sizing;
      return {notional:Number(a.position_usdt || 0).toFixed(0),effLev:Number(a.effective_leverage || 0).toFixed(2),riskUsd:Number(a.risk_usd || 0).toFixed(2),profitUsd:Number(a.target_profit_usd || 0).toFixed(2),slPct:Number(a.sl_pct || 0).toFixed(2),tpPct:Number(a.tp_pct || 0).toFixed(2)};
    }
    return calcSizing(item.entry,item.stop,item.tp);
  }

  function statusClass(status) {
    if (status === 'IN_TRADE') return 'ms-pill-intrade';
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
    const sz = displayedSizing(item);

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
    const sz = displayedSizing(item);
    const sideCls = item.side === 1 ? 'ms-side-long' : item.side === -1 ? 'ms-side-short' : 'ms-side-flat';
    function buildActionBannerHtml(obj) {
      if (!obj) return '';
      const alert = obj.action_alert;
      if (alert && alert.title_ua) {
        const cls = alert.level === 'success' ? 'ms-alert-success' : 'ms-alert-warn';
        return `<div class="ms-action-banner ms-hl-action-banner ${cls}">${alert.title_ua}${alert.detail_ua ? `<small>${alert.detail_ua}</small>` : ''}</div>`;
      }
      if (obj.tp1_hit) {
        return `<div class="ms-action-banner ms-hl-action-banner ms-alert-success">⚡ УВАГА: ФІКСУЄМО 50% (TP1) · СТОП ПЕРЕНЕСЕНО В Б/У (${fmtPrice(obj.stop, obj.tick)})<small>Решта 50% іде як Runner до TP ${fmtPrice(obj.tp, obj.tick)}.</small></div>`;
      }
      if (obj.stale_tightened) {
        const rem = obj.time_stop_countdown_min ? ` · закриття через ~${obj.time_stop_countdown_min} хв` : '';
        return `<div class="ms-action-banner ms-hl-action-banner ms-alert-warn">⏱ УВАГА: СТОП ПЕРЕНЕСЕНО (ТАЙМ-СТОП)${rem}<small>Угода без імпульсу (Alpha Decay). Новий стоп: ${fmtPrice(obj.stop, obj.tick)}.</small></div>`;
      }
      if (obj.time_stop_countdown_min) {
        return `<div class="ms-action-banner ms-hl-action-banner ms-alert-warn">⏱ УВАГА: УГОДА БУДЕ ЗАКРИТА ЧЕРЕЗ ~${obj.time_stop_countdown_min} ХВ (НЕМАЄ ІМПУЛЬСУ)<small>Alpha-Decay Time-Stop: якщо ціна не дасть розвитку, закриваємо достроково.</small></div>`;
      }
      if (obj.funding_regime === 'CROWDED_WITH_US') {
        return `<div class="ms-action-banner ms-hl-action-banner ms-alert-warn">⚠ ПЕРЕГРІТИЙ FUNDING (${Number(obj.funding_pct || 0).toFixed(3)}%) · ДОСТРОКОВА ФІКСАЦІЯ TP1 (+${Number(obj.tp1_r || 0.85).toFixed(2)} R)<small>Ризик знижено (×0.75). При досягненні TP1 фіксуємо 50% і переводимо в Б/У.</small></div>`;
      }
      return '';
    }
    const hlBannerHtml = item.status === 'IN_TRADE' || item.funding_regime === 'CROWDED_WITH_US' ? buildActionBannerHtml(item) : '';
    const tp1Txt = item.tp1 ? `TP1 (50%): ${fmtPrice(item.tp1, item.tick)}` : '';
    const orderLbl = item.status === 'IN_TRADE'
      ? (item.phase === 'PENDING_LIMIT' ? 'ЛІМІТКА ОЧІКУЄ' : 'АКТИВНА ПОЗИЦІЯ')
      : item.status === 'READY'
        ? (item.order_type === 'LIMIT' ? 'ЛІМІТКА (PAPER)' : 'РИНКОВИЙ ВХІД (M1/M5)')
        : 'ЩЕ НЕ ВХОДИМО (ОЧІКУВАННЯ)';
    const badgeText = isTopPick ? '★ №1 НАЙКРАЩИЙ ВИБІР' : 'ВИБРАНИЙ АКТИВ';

    card.innerHTML = `
      <div class="ms-hl-top">
        <div class="ms-hl-badges">
          <span class="ms-top-tag">${badgeText}</span>
          <span class="ms-cl ${clusterClass(item.cluster)}">${item.cluster_ua}</span>
          <span class="ms-fee">${item.fee_tag || 'FEE: EST.'}</span>
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
      ${hlBannerHtml}
      <div class="ms-hl-levels">
        <div class="ms-lvl"><small>ВХІД${item.maker_pullback ? ' · MAKER 0%' : ''}</small><strong>${fmtPrice(item.entry, item.tick)}</strong></div>
        <div class="ms-lvl"><small>СТОП-ЛОСС${item.be_armed ? ' (Б/У)' : item.stale_tightened ? ' (ТАЙМ-СТОП)' : ''}</small><strong class="${item.be_armed ? 'ms-up' : 'ms-dn'}">${fmtPrice(item.stop, item.tick)}${sz && item.status !== 'IN_TRADE' ? ' (−' + sz.slPct + '%)' : ''}</strong></div>
        <div class="ms-lvl"><small>TP2 (RUNNER)${tp1Txt ? ' · ' + tp1Txt : ''}</small><strong class="ms-up">${fmtPrice(item.tp, item.tick)}${sz ? ' (+' + sz.tpPct + '%)' : ''}</strong></div>
        <div class="ms-lvl"><small>R:R / RVOL${item.funding_pct ? ' · FR ' + Number(item.funding_pct).toFixed(3) + '%' : ''}</small><strong>${item.rr ? item.rr.toFixed(2) + ' R' : '—'} · ${item.rvol ? item.rvol.toFixed(2) + '×' : '—'}</strong></div>
      </div>
      ${sz ? `
      <div class="ms-sizing-bar">
        <div><small>ПОЗИЦІЯ</small><strong>$${sz.notional}</strong></div>
        <div><small>ПЛЕЧЕ</small><strong>${sz.effLev}×</strong></div>
        <div><small>ПОЧАТКОВИЙ РИЗИК</small><strong class="ms-dn">−$${sz.riskUsd}</strong></div>
        <div><small>ВАЛОВИЙ TP</small><strong class="ms-up">+$${sz.profitUsd}</strong></div>
      </div>` : ''}
      <p class="ms-hl-reason">${item.reason_ua || ''}${item.meta ? ` · Meta: ${item.meta.n || 0} історичних аналогів${item.meta.expected_r_estimate === null ? ' (збір даних)' : ' (невалідована оцінка)'}` : ''}${item.geometry_policy === 'STRUCTURAL_V15_1' ? ` · Структурний SL${item.target_kind === 'CONFIRMED_LIQUIDITY' ? ' · TP: ліквідність' : item.target_kind === 'VOLATILITY_PROJECTION' ? ' · TP: розрахункова проєкція' : ''}` : ''}${item.lifecycle_quality && item.lifecycle_quality !== 'OBSERVED_M1' ? ` · Якість обліку: ${item.lifecycle_quality}` : ''}</p>
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
      const liveTag = ws && ws.readyState === 1 ? ' · WS: ціни' : '';
      const health = data.health || {};
      const age = data.updated_ts ? Math.max(0, Math.floor((Date.now()-Number(data.updated_ts))/1000)) : null;
      const interval = Number(health.expected_interval_sec || 300);
      const stale = age === null || age > interval + 60 || scannerFetchError || ['FAILED_DATA','FAILED_CYCLE'].includes(health.status);
      const ageTag = age === null ? 'немає даних' : `${Math.floor(age/60)} хв ${age%60} с тому`;
      const sourceTag = snapshotSource ? ` · ${snapshotSource}` : '';
      const attempt = data.last_attempt_ts && data.last_attempt_ts > data.updated_ts ? ` · спроба ${new Date(data.last_attempt_ts).toLocaleTimeString('uk-UA',{timeZone:'Europe/Kyiv'})}` : '';
      const denied = health.status === 'FAILED_DATA' && (health.access_denied || (data.errors || []).some(e => /HTTP Error (401|403|451)/.test(e)));
      const statusLabel = denied ? '⚠ MEXC: ВІДМОВА API' : health.status === 'FAILED_DATA' ? '⚠ MEXC: НЕМАЄ ДАНИХ' : stale ? '⚠ ЗАСТАРІЛИЙ АНАЛІЗ' : health.status === 'DEGRADED' ? '⚠ ЧАСТКОВІ ДАНІ' : 'Аналіз';
      const perf = health.timings && Number.isFinite(health.timings.scan_s) ? ` · аналіз ${health.timings.scan_s.toFixed(1)} с` : '';
      updEl.textContent = `${statusLabel}: ${data.updated_kyiv || '—'} Київ · ${ageTag} · цикл ${interval/60} хв${perf}${health.errors ? ' · помилок '+health.errors : ''}${Number.isInteger(health.fresh_quotes) ? ' · REST '+health.fresh_quotes+'/7' : ''}${scannerFetchError ? ' · помилка завантаження' : ''}${attempt}${sourceTag}${liveTag}`;
      updEl.classList.toggle('ms-dn',stale);
      updEl.title = [(data.errors || []).join('\n'),health.last_error, health.api_base ? `REST: ${health.api_base}` : '', health.fanout_skipped ? 'Запити зупинені після двох відмов API' : '', health.run_id ? `GitHub run: ${health.run_id}` : '', health.recovered?.length ? `Відновлено: ${health.recovered.join(', ')}` : '', health.derived?.length ? `Агреговано: ${health.derived.join(', ')}` : '',fetchDetails].filter(Boolean).join('\n');

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

  function _renderSlotActionBanner(alertEl, act) {
    if (!alertEl) return;
    if (!act) { alertEl.hidden = true; alertEl.innerHTML = ''; return; }
    const tick = act.tick || 0.01;
    let title = '', detail = '', level = 'warn';
    if (act.action_alert && act.action_alert.title_ua) {
      title = act.action_alert.title_ua;
      detail = act.action_alert.detail_ua || '';
      level = act.action_alert.level === 'success' ? 'success' : 'warn';
    } else if (act.tp1_hit) {
      title = `⚡ УВАГА: ФІКСУЄМО 50% (TP1) · СТОП ПЕРЕНЕСЕНО В Б/У (${fmtPrice(act.stop, tick)})`;
      detail = `Решта 50% іде як Runner до TP ${fmtPrice(act.tp, tick)}.`;
      level = 'success';
    } else if (act.stale_tightened) {
      const rem = act.time_stop_countdown_min ? ` · закриття через ~${act.time_stop_countdown_min} хв` : '';
      title = `⏱ УВАГА: СТОП ПЕРЕНЕСЕНО (ТАЙМ-СТОП)${rem}`;
      detail = `Немає імпульсу (Alpha Decay). Стоп підтягнуто до ${fmtPrice(act.stop, tick)}.`;
      level = 'warn';
    } else if (act.time_stop_countdown_min) {
      title = `⏱ УВАГА: УГОДА БУДЕ ЗАКРИТА ЧЕРЕЗ ~${act.time_stop_countdown_min} ХВ (НЕМАЄ ІМПУЛЬСУ)`;
      detail = 'Alpha-Decay Time-Stop: дострокове закриття при відсутності розвитку.';
      level = 'warn';
    } else if (act.funding_regime === 'CROWDED_WITH_US') {
      title = `⚠ ПЕРЕГРІТИЙ FUNDING · ДОСТРОКОВА ФІКСАЦІЯ TP1 (+${Number(act.tp1_r || 0.85).toFixed(2)} R)`;
      detail = 'Ризик знижено (×0.75) · фіксуємо 50% раніше та переводимо в Б/У.';
      level = 'warn';
    }
    if (!title) { alertEl.hidden = true; alertEl.innerHTML = ''; return; }
    alertEl.hidden = false;
    alertEl.className = `ms-action-banner ${level === 'success' ? 'ms-alert-success' : 'ms-alert-warn'}`;
    alertEl.innerHTML = `${title}${detail ? `<small>${detail}</small>` : ''}`;
  }

  function _renderSlotCard(cardId, badgeId, titleId, subId, alertId, slotObj, emptyTitle, emptySub) {
    const card = $(cardId), badge = $(badgeId), title = $(titleId), sub = $(subId), alertEl = $(alertId);
    if (!card || !badge || !title || !sub) return;
    card.classList.remove('ms-slot-pos', 'ms-slot-neg', 'ms-slot-free');
    const act = slotObj && slotObj.active;
    if (act && posNum(act.entry)) {
      const side = Number(act.side || 1);
      const sideTxt = side === 1 ? 'LONG' : 'SHORT';
      const tick = act.tick || 0.01;
      if (act.phase === 'PENDING_LIMIT') {
        badge.textContent = act.maker_pullback ? 'MAKER ЛІМІТКА (0%)' : 'ЛІМІТКА ОЧІКУЄ';
        title.textContent = `${act.label} · ${sideTxt} @ ${fmtPrice(act.entry, tick)} $`;
        sub.textContent = `SL ${fmtPrice(act.stop, tick)} · TP1 ${fmtPrice(act.tp1 || act.tp, tick)} · TP2 ${fmtPrice(act.tp, tick)} · до ${act.maker_pullback ? '15' : '8'} хв`;
        _renderSlotActionBanner(alertEl, act);
        return;
      }
      const entry = Number(act.entry);
      const stop = posNum(act.stop);
      const tp = posNum(act.tp);
      const risk = posNum(act.risk) || (stop ? Math.abs(entry - stop) : 0);
      const curP = posNum(livePrices[act.symbol]) || posNum(act.current_price) || entry;
      const cost = risk > 0 ? (Number(act.fee_rate || 0) + Number(act.slippage_rate || 0)) * (entry + curP) / risk : 0;
      let pnlR = Number(act.pnl_r || 0);
      if (risk > 0) {
        if (act.tp1_hit && posNum(act.tp1_fill_price)) {
          const pp = Number(act.partial_pct || 0.5);
          const g1 = side * (Number(act.tp1_fill_price) - entry) / risk;
          const g2 = side * (curP - entry) / risk;
          pnlR = pp * g1 + (1 - pp) * g2 - cost;
        } else {
          pnlR = side * (curP - entry) / risk - cost;
        }
      }
      const riskUsd = Number((act.sizing && act.sizing.risk_usd) || 3);
      const pnlUsd = pnlR * riskUsd;
      card.classList.add(pnlUsd >= 0 ? 'ms-slot-pos' : 'ms-slot-neg');
      badge.textContent = `${act.tp1_hit ? 'TP1 50% ✓ · ' : 'В УГОДІ · '}${fmtSigned(pnlR)} R (${fmtSigned(pnlUsd)}$)`;
      title.textContent = `${act.label} · ${sideTxt} @ ${fmtPrice(entry, tick)} $`;
      sub.textContent = `Ціна ${fmtPrice(curP, tick)} · SL ${fmtPrice(stop, tick)}${act.tp1 && !act.tp1_hit ? ' · TP1 ' + fmtPrice(act.tp1, tick) : ''} · TP2 ${fmtPrice(tp, tick)}${act.tp1_hit ? ' · Б/У + Runner 50%' : act.stale_tightened ? ' · Тайм-стоп SL' : act.trail_reason ? ' · Структурний SL' : act.be_armed ? ' · Б/У' : ''}`;
      _renderSlotActionBanner(alertEl, act);
    } else {
      card.classList.add('ms-slot-free');
      badge.textContent = 'ВІЛЬНО';
      title.textContent = emptyTitle;
      sub.textContent = emptySub;
      _renderSlotActionBanner(alertEl, null);
    }
  }

  function renderDualSlotsAndDaily() {
    const oilSlot = (latestData && latestData.oil_slot) || {};
    const cmSlot = (latestData && latestData.cm_slot) || {};

    _renderSlotCard(
      'ms-slot-oil', 'ms-slot-oil-badge', 'ms-slot-oil-title', 'ms-slot-oil-sub', 'ms-slot-oil-alert',
      oilSlot,
      'Немає відкритої угоди по нафті',
      'Слот 1 вільний · очікуємо A+ сигнал UKOIL на MEXC'
    );
    _renderSlotCard(
      'ms-slot-cm', 'ms-slot-cm-badge', 'ms-slot-cm-title', 'ms-slot-cm-sub', 'ms-slot-cm-alert',
      cmSlot,
      'Немає відкритої угоди по крипті / металу',
      'Слот 2 вільний · скануємо SOL · LINK · LTC · APT · ORDI · XPT'
    );

    const oilD = oilSlot.daily_all || oilSlot.daily || { trades: 0, net_r: 0, net_usd: 0 };
    const cmD = cmSlot.daily_all || cmSlot.daily || { trades: 0, net_r: 0, net_usd: 0 };
    const oilT = Number(oilD.trades || 0), oilR = Number(oilD.net_r || 0), oilU = Number(oilD.net_usd || 0);
    const cmT = Number(cmD.trades || 0), cmR = Number(cmD.net_r || 0), cmU = Number(cmD.net_usd || 0);

    const dOil = $('ms-day-oil'), dCm = $('ms-day-cm'), dTot = $('ms-day-total');
    if (dOil) {
      dOil.textContent = `${oilT} уг. · ${fmtSigned(oilR)} R (${fmtSigned(oilU)}$)`;
      dOil.className = oilU > 0 ? 'ms-up' : oilU < 0 ? 'ms-dn' : '';
    }
    if (dCm) {
      dCm.textContent = `${cmT} уг. · ${fmtSigned(cmR)} R (${fmtSigned(cmU)}$)`;
      dCm.className = cmU > 0 ? 'ms-up' : cmU < 0 ? 'ms-dn' : '';
    }
    if (dTot) {
      const totT = oilT + cmT, totR = oilR + cmR, totU = oilU + cmU;
      dTot.textContent = `${totT} уг. · ${fmtSigned(totR)} R (${fmtSigned(totU)}$)`;
      dTot.className = totU > 0 ? 'ms-up' : totU < 0 ? 'ms-dn' : '';
    }
    if (historyModalOpen) renderHistoryModal();
  }


  // === Sound Alerts (Web Audio API) & Slot Blinking ===
  let soundEnabled = false;
  let audioCtx = null;
  let prevSlotState = {
    oilOpenTs: null,
    oilClosedTs: null,
    oilAlertTs: null,
    cmOpenTs: null,
    cmClosedTs: null,
    cmAlertTs: null,
    initialized: false,
  };

  function ensureAudio() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
    return audioCtx;
  }

  function playNotes(notes) {
    if (!soundEnabled) return;
    const ctx = ensureAudio();
    if (!ctx) return;
    const now = ctx.currentTime;
    notes.forEach(([freq, delay, dur]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, now + delay);
      gain.gain.setValueAtTime(0.001, now + delay);
      gain.gain.exponentialRampToValueAtTime(0.18, now + delay + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, now + delay + dur);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + delay);
      osc.stop(now + delay + dur + 0.02);
    });
  }

  function playSound(kind) {
    if (kind === 'entry') {
      playNotes([[523.25, 0.0, 0.16], [659.25, 0.14, 0.16], [783.99, 0.28, 0.28]]);
    } else if (kind === 'win') {
      playNotes([[783.99, 0.0, 0.16], [1046.5, 0.16, 0.34]]);
    } else if (kind === 'loss') {
      playNotes([[329.63, 0.0, 0.18], [261.63, 0.18, 0.30]]);
    } else if (kind === 'action') {
      playNotes([[659.25, 0.0, 0.14], [880.0, 0.14, 0.16], [987.77, 0.30, 0.24]]);
    }
  }

  function triggerBlink(cardId, cls) {
    const card = $(cardId);
    if (!card) return;
    card.classList.remove('ms-slot-blink-entry', 'ms-slot-blink-win', 'ms-slot-blink-loss', 'ms-slot-blink-action');
    void card.offsetWidth;
    card.classList.add(cls);
  }

  function checkSlotAlerts(data) {
    if (!data) return;
    const oilOpenTs = data.oil_slot?.active?.opened_ts || null;
    const oilClosedTs = data.oil_slot?.last_closed?.closed_ts || null;
    const oilClosedUsd = Number(data.oil_slot?.last_closed?.net_usd ?? data.oil_slot?.last_closed?.net_r ?? 0);
    const oilAlertTs = data.oil_slot?.active?.action_alert?.ts || null;
    const cmOpenTs = data.cm_slot?.active?.opened_ts || null;
    const cmClosedTs = data.cm_slot?.last_closed?.closed_ts || null;
    const cmClosedUsd = Number(data.cm_slot?.last_closed?.net_usd ?? data.cm_slot?.last_closed?.net_r ?? 0);
    const cmAlertTs = data.cm_slot?.active?.action_alert?.ts || null;

    if (prevSlotState.initialized) {
      if (oilOpenTs && oilOpenTs !== prevSlotState.oilOpenTs) {
        triggerBlink('ms-slot-oil', 'ms-slot-blink-entry');
        playSound('entry');
      }
      if (cmOpenTs && cmOpenTs !== prevSlotState.cmOpenTs) {
        triggerBlink('ms-slot-cm', 'ms-slot-blink-entry');
        playSound('entry');
      }
      if (oilAlertTs && oilAlertTs !== prevSlotState.oilAlertTs) {
        triggerBlink('ms-slot-oil', 'ms-slot-blink-action');
        playSound('action');
      }
      if (cmAlertTs && cmAlertTs !== prevSlotState.cmAlertTs) {
        triggerBlink('ms-slot-cm', 'ms-slot-blink-action');
        playSound('action');
      }
      if (oilClosedTs && oilClosedTs !== prevSlotState.oilClosedTs) {
        triggerBlink('ms-slot-oil', oilClosedUsd >= 0 ? 'ms-slot-blink-win' : 'ms-slot-blink-loss');
        playSound(oilClosedUsd >= 0 ? 'win' : 'loss');
      }
      if (cmClosedTs && cmClosedTs !== prevSlotState.cmClosedTs) {
        triggerBlink('ms-slot-cm', cmClosedUsd >= 0 ? 'ms-slot-blink-win' : 'ms-slot-blink-loss');
        playSound(cmClosedUsd >= 0 ? 'win' : 'loss');
      }
    }
    prevSlotState = { oilOpenTs, oilClosedTs, oilAlertTs, cmOpenTs, cmClosedTs, cmAlertTs, initialized: true };
  }

  // === Interactive Trade History Modal ===
  let historyModalOpen = false;
  let historySlotFilter = 'ALL';
  let historyPeriodFilter = 'TODAY';

  const REASON_UA_MAP = {
    TAKE_PROFIT: 'Тейк-профіт (TP)',
    TP1_RUNNER_TP: 'TP1 (50%) + Runner TP',
    TP1_BE_STOP: 'TP1 (50%) + Б/У залишку',
    BE_STOP: 'Беззбиток / Структурний трейлінг',
    STOP: 'Стоп-лосс',
    STALE_EXIT: 'Дострокове закриття (Тайм-стоп)',
    TIME_EXIT: 'Закриття за часом (18 год)',
  };

  function getClosedHistoryRows() {
    if (!latestData) return [];
    if (Array.isArray(latestData.closed_history) && latestData.closed_history.length) {
      return latestData.closed_history;
    }
    const oilH = (latestData.oil_slot?.history || []).map((h) => ({ ...h, slot: h.slot || 'OIL' }));
    const cmH = (latestData.cm_slot?.history || []).map((h) => ({ ...h, slot: h.slot || 'CRYPTO_METAL' }));
    return [...oilH, ...cmH].sort((a, b) => Number(b.closed_ts || 0) - Number(a.closed_ts || 0));
  }

  function _todayPrefixKyiv() {
    const dayStr = latestData?.oil_slot?.daily_all?.day || latestData?.cm_slot?.daily_all?.day || '';
    if (/^\d{4}-\d{2}-\d{2}$/.test(dayStr)) {
      const [, m, d] = dayStr.split('-');
      return `${d}.${m}`;
    }
    return '';
  }

  function renderHistoryModal() {
    const tbody = $('ms-history-rows');
    const kpis = $('ms-history-kpis');
    if (!tbody || !kpis) return;
    let rows = getClosedHistoryRows();
    if (historySlotFilter !== 'ALL') {
      rows = rows.filter((r) => (r.slot || (r.symbol === 'UKOIL_USDT' ? 'OIL' : 'CRYPTO_METAL')) === historySlotFilter);
    }
    const todayPref = _todayPrefixKyiv();
    if (historyPeriodFilter === 'TODAY' && todayPref) {
      rows = rows.filter((r) => String(r.closed_kyiv || '').startsWith(todayPref));
    }

    const total = rows.length;
    const wins = rows.filter((r) => Number(r.net_usd || 0) > 0).length;
    const losses = rows.filter((r) => Number(r.net_usd || 0) < 0).length;
    const wr = total > 0 ? Math.round((wins / total) * 100) : 0;
    const sumR = rows.reduce((acc, r) => acc + Number(r.net_r || 0), 0);
    const sumU = rows.reduce((acc, r) => acc + Number(r.net_usd || 0), 0);
    const uCls = sumU > 0 ? 'ms-up' : sumU < 0 ? 'ms-dn' : '';

    kpis.innerHTML = `
      <div class="ms-hkpi"><small>ЗАКРИТО УГОД</small><strong>${total} (${wins}W / ${losses}L)</strong></div>
      <div class="ms-hkpi"><small>ВІНРЕЙТ (ПО $)</small><strong>${wr}%</strong></div>
      <div class="ms-hkpi"><small>ЧИСТИЙ R</small><strong>${fmtSigned(sumR)} R</strong></div>
      <div class="ms-hkpi"><small>РЕАЛЬНИЙ ПІДСУМОК ($)</small><strong class="${uCls}">${fmtSigned(sumU)}$</strong></div>
    `;

    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="ms-empty">Немає закритих угод за вибраний період.</td></tr>';
      return;
    }

    tbody.innerHTML = rows.map((r) => {
      const tick = r.symbol && (r.symbol.includes('OIL') || r.symbol.includes('XPT')) ? 0.01 : 0.001;
      const usd = Number(r.net_usd || 0);
      const nr = Number(r.net_r || 0);
      const resCls = usd > 0 ? 'ms-up' : usd < 0 ? 'ms-dn' : '';
      const reasonCode = String(r.reason || '');
      const reasonTxt = r.reason_ua || REASON_UA_MAP[reasonCode] || reasonCode || '—';
      const pillCls = (reasonCode === 'TAKE_PROFIT' || reasonCode === 'TP1_RUNNER_TP' || usd > 0)
        ? 'ms-reason-win'
        : (reasonCode === 'STALE_EXIT' || reasonCode === 'BE_STOP' || reasonCode === 'TP1_BE_STOP' || reasonCode === 'TIME_EXIT')
          ? 'ms-reason-warn'
          : 'ms-reason-loss';
      const sideCls = Number(r.side || 1) === 1 ? 'ms-up' : 'ms-dn';
      const setupTxt = r.setup_ua || r.setup || '—';
      return `
        <tr>
          <td><strong>${r.closed_kyiv || '—'}</strong>${r.opened_kyiv ? `<small>відкр. ${r.opened_kyiv}</small>` : ''}</td>
          <td><strong>${r.label || r.symbol}</strong> · <span class="${sideCls}">${r.side_ua || (r.side === 1 ? 'LONG ↑' : 'SHORT ↓')}</span><small>${setupTxt}${r.tp1_hit ? ' · ⚡ TP1 50%' : ''}</small></td>
          <td class="ms-num">${fmtPrice(r.entry, tick)} → ${fmtPrice(r.exit, tick)}</td>
          <td class="ms-num ${resCls}"><strong>${fmtSigned(usd)}$</strong><small>${fmtSigned(nr)} R</small></td>
          <td><span class="ms-reason-pill ${pillCls}">${reasonTxt}</span></td>
        </tr>
      `;
    }).join('');
  }

  function openHistoryModal(slotFilter = 'ALL') {
    const modal = $('ms-history-modal');
    if (!modal) return;
    historySlotFilter = slotFilter || 'ALL';
    historyModalOpen = true;
    modal.hidden = false;
    modal.setAttribute('aria-hidden', 'false');
    document.querySelectorAll('[data-hslot]').forEach((b) => {
      b.setAttribute('aria-pressed', b.getAttribute('data-hslot') === historySlotFilter ? 'true' : 'false');
    });
    renderHistoryModal();
  }

  function closeHistoryModal() {
    const modal = $('ms-history-modal');
    if (!modal) return;
    historyModalOpen = false;
    modal.hidden = true;
    modal.setAttribute('aria-hidden', 'true');
  }

  function wireHistoryModal() {
    const modal = $('ms-history-modal');
    if (!modal) return;
    document.querySelectorAll('.ms-daily-clickable').forEach((el) => {
      const open = () => openHistoryModal(el.getAttribute('data-slot-filter') || 'ALL');
      el.addEventListener('click', open);
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    const closeBtn = $('ms-history-close');
    if (closeBtn) closeBtn.addEventListener('click', closeHistoryModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeHistoryModal(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && historyModalOpen) closeHistoryModal(); });
    document.querySelectorAll('[data-hslot]').forEach((btn) => {
      btn.addEventListener('click', () => {
        historySlotFilter = btn.getAttribute('data-hslot') || 'ALL';
        document.querySelectorAll('[data-hslot]').forEach((b) => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
        renderHistoryModal();
      });
    });
    document.querySelectorAll('[data-hperiod]').forEach((btn) => {
      btn.addEventListener('click', () => {
        historyPeriodFilter = btn.getAttribute('data-hperiod') || 'TODAY';
        document.querySelectorAll('[data-hperiod]').forEach((b) => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
        renderHistoryModal();
      });
    });
  }

  function wireSoundButton() {
    const btn = $('ms-sound-btn');
    if (!btn) return;
    try {
      soundEnabled = localStorage.getItem('bzu_multi_sound') === '1';
    } catch (_) {}
    const syncBtn = () => {
      btn.setAttribute('aria-pressed', soundEnabled ? 'true' : 'false');
      btn.textContent = soundEnabled ? '🔔 Звук: увімк.' : '🔕 Звук: вимк.';
    };
    syncBtn();
    btn.addEventListener('click', () => {
      soundEnabled = !soundEnabled;
      try { localStorage.setItem('bzu_multi_sound', soundEnabled ? '1' : '0'); } catch (_) {}
      syncBtn();
      if (soundEnabled) {
        ensureAudio();
        playSound('entry');
      }
    });
  }

  function renderAll(updateChart = false) {
    if (!latestData) return;
    renderDualSlotsAndDaily();
    renderMeta(latestData);
    renderTable(latestData, updateChart);
  }

  async function loadScannerState() {
    if (scanLoading) return;
    scanLoading = true;
    const endpoints = [{url:STATE_URL,label:'Pages / worker'}];
    // Public repository snapshot, bypassing the Pages deployment queue. Never send credentials.
    if (/^https:\/\/raw\.githubusercontent\.com\//.test(RAW_STATE_URL)) endpoints.push({url:RAW_STATE_URL,label:'GitHub state'});
    let success = 0; const failures=[];
    async function poll({url,label}) {
      const controller = new AbortController();
      const timer=setTimeout(()=>controller.abort(),6000);
      try {
        const response=await fetch(`${url}${url.includes('?')?'&':'?'}t=${Math.floor(Date.now()/15000)}`, {cache:'no-store',signal:controller.signal,credentials:'omit'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data=await response.json();
        if (!Array.isArray(data.instruments) || !Number.isFinite(snapshotClock(data)) || snapshotClock(data)<=0) throw new Error('Invalid snapshot');
        success++;
        const first=!latestData;
        // Failed-cycle heartbeat can be newer than the last successful analysis.
        if (!latestData || snapshotClock(data)>snapshotClock(latestData) || (snapshotClock(data)===snapshotClock(latestData) && Number(data.updated_ts||0)>Number(latestData.updated_ts||0))) {
          latestData=data; snapshotSource=label; scannerFetchError=false;
          checkSlotAlerts(latestData); renderAll(first || chartMode==='levels');
        }
      } catch (e) { failures.push(`${label}: ${e.name==='AbortError'?'timeout':e.message}`); }
      finally { clearTimeout(timer); }
    }
    try {
      await Promise.all(endpoints.map(poll));
      scannerFetchError=success===0;
      fetchDetails=failures.join('\n');
      if(latestData) renderMeta(latestData);
      else {const el=$('ms-updated'); if(el) el.textContent='⚠ Немає зліпка MEXC — перевірте worker / GitHub Actions';}
    } finally { scanLoading=false; }
  }

  function wireBannerClick() {
    const oilCard = $('ms-slot-oil');
    if (oilCard) {
      oilCard.addEventListener('click', () => {
        oilCard.classList.remove('ms-slot-blink-entry', 'ms-slot-blink-win', 'ms-slot-blink-loss', 'ms-slot-blink-action');
        selectedSymbol = 'UKOIL_USDT';
        renderAll(true);
        const sec = $('multi-scanner-section');
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
    const cmCard = $('ms-slot-cm');
    if (cmCard) {
      cmCard.addEventListener('click', () => {
        cmCard.classList.remove('ms-slot-blink-entry', 'ms-slot-blink-win', 'ms-slot-blink-loss', 'ms-slot-blink-action');
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
    wireSoundButton();
    wireHistoryModal();
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
    setInterval(() => { if (latestData) renderMeta(latestData); }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
