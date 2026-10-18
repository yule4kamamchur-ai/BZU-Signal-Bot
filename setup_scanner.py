"""Native candle contexts and explicit forming conditions for 24 ICT setups.

Readiness is a fraction of observed prerequisites. Only the ORIGINAL detector
can confirm a setup. It is never a success probability or an execution signal.
"""
from __future__ import annotations

from statistics import mean
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

PRIMARY = ('15m', '1H', '4H')
MICRO = {'15m': '3m', '1H': '15m', '4H': '1H'}
MINUTES = {'15m': 15, '1H': 60, '4H': 240}


def passive_gate(entry, side, bid, ask, tick):
    """Marketable limits cross the opposite quote, not the last trade price."""
    e=Decimal(str(entry));t=Decimal(str(tick))
    quote=ask if side==1 else bid
    passive=entry<ask if side==1 else entry>bid
    boundary=((e/t).to_integral_value(rounding=ROUND_FLOOR)+1)*t if side==1 else ((e/t).to_integral_value(rounding=ROUND_CEILING)-1)*t
    return {'passive':passive,'quote_name':'ASK' if side==1 else 'BID',
            'current_quote':quote,'required_quote':float(boundary),
            'operator':'≥' if side==1 else '≤'}


def distance_details(price, entry, tick):
    gap=abs(Decimal(str(price))-Decimal(str(entry)))
    percentage=float(gap/Decimal(str(price))*100) if price>0 else None
    ticks=float(gap/Decimal(str(tick))) if tick>0 else None
    return {'at_level':gap==0,'distance':float(gap),'distance_pct':percentage,'ticks':ticks}


def distance_text(price, entry, tick):
    detail=distance_details(price,entry,tick)
    if detail['at_level']:
        return f'📍 Ціна на рівні входу {entry:.8g} • 0 тиків'
    pct=detail['distance_pct']
    value=f'{pct:.3f}%' if pct>=.0005 else '<0.001%'
    ticks=detail['ticks']
    tick_text=str(round(ticks)) if abs(ticks-round(ticks))<1e-8 else f'{ticks:.6f}'.rstrip('0').rstrip('.')
    if tick_text=='0' and ticks>0:
        tick_text='<0.000001'
    return f"📏 До входу: {value} • {detail['distance']:.8g} USDT • {tick_text} тиків"


def wait_text(entry, side, bid, ask, tick):
    gate=passive_gate(entry,side,bid,ask,tick)
    return (f"🚦 Ліміт {'LONG' if side==1 else 'SHORT'} {entry:.8g}: чекаємо "
            +f"{gate['quote_name']} {gate['operator']} {gate['required_quote']:.8g} "
            +f"(зараз {gate['current_quote']:.8g})")

ENTRY_CHECKS = (
    ('PASSIVE_LIMIT','Ліміт стоїть на правильному боці ціни'),
    ('FRESH_LEVEL','Рівень актуальний і ще не відвіданий'),
    ('VALID_STOP','Структурний SL відповідає межам ризику'),
    ('HTF_ALIGNMENT','Старші графіки допускають цей напрямок'),
    ('TARGET_RR','Потенціал TP достатній відносно SL'),
    ('CONTEXT_SCORE','Якість контексту проходить поріг'),
    ('STRUCTURAL_CONFIRMATION','Структурні умови входу підтверджені'),
    ('DECISION_EVALUATED','Сценарій оцінено без порогу win rate'),
)


def entry_checks():
    return [{'key':key,'condition':label,'met':None} for key,label in ENTRY_CHECKS]


def update_readiness(row):
    """Count setup prerequisites AND actual admission checks, not distance.

    None means not evaluated yet and does not count as passed. Portfolio
    capacity is separate: an otherwise ready setup stays ready while busy.
    """
    pattern=row.get('setup_conditions',row.get('conditions',[]))
    checks=row.setdefault('entry_checks',entry_checks())
    passed=sum(c.get('met') is True for c in pattern+checks)
    total=len(pattern)+len(checks)
    row['setup_readiness_pct']=round(100*sum(c.get('met') is True for c in pattern)/len(pattern),1) if pattern else 0.
    row['readiness_pct']=round(100*passed/total,1) if total else 0.
    row['met_conditions']=passed
    row['total_conditions']=total
    row['missing_conditions']=[c['condition'] for c in pattern+checks if c.get('met') is not True]
    return row


def selection_key(row, price=None):
    """Same deterministic ranking for the book, Telegram and dashboard.

    Completion is first. With equal readiness use independent historical
    confidence when available, then context quality and price distance.
    An unvalidated estimate is never used as an invented win probability.
    """
    ready=row.get('readiness_pct',100. if row.get('quality',{}).get('accepted') else 0.) or 0.
    stat=row.get('statistics',row.get('quality',{}).get('statistics',{}))
    adjustment=stat.get('ranking_adjustment',0.)
    level=row.get('level',row.get('entry'))
    distance=proximity(price,level)['distance_pct'] if price else row.get('distance_pct')
    tf=row.get('timeframe',row.get('setup_timeframe','15m'))
    return (float(ready),float(row.get('decision_score',float(row.get('score',0) or 0)+adjustment)),float(row.get('score',0) or 0),
            -float(distance) if distance is not None else float('-inf'),
            MINUTES.get(tf,15),str(row.get('setup','')),str(row.get('id',row.get('event_key','')) or ''))


def best_scenario(rows, price=None, sides=None):
    eligible=[r for r in rows if r.get('status') not in ('ERROR','FOLLOWED')
              and r.get('side') in (-1,1) and (sides is None or r.get('side') in sides)]
    return max(eligible,key=lambda r:selection_key(r,price),default=None)


def timeframe(plan):
    tf = plan.get('setup_timeframe', plan.get('timeframe', '15m'))
    return tf if tf in PRIMARY else '15m'


def build_contexts(snapshot, base, state, cfg):
    """Reuse detector slot names, with real bars and volatility of each TF.

    Session references ALWAYS come from real 15m data: four 4H candles are
    never passed off as an opening range. SMT peers exist only for real 15m.
    """
    core = cfg.core
    out = {'15m': dict(base, analysis_timeframe='15m', micro_timeframe='3m')}
    for tf in PRIMARY[1:]:
        micro = MICRO[tf]
        bars = {**snapshot.candles, '15m': snapshot.candles[tf],
                '3m': snapshot.candles[micro]}
        data = {**snapshot.metadata, 'price': snapshot.price,
                'spread': snapshot.ask-snapshot.bid, 'candles': bars,
                'execution_price_trusted': snapshot.trusted,
                'ticker_ts': snapshot.ticker_ts, 'instrument': cfg.instrument,
                'htf_source': {t: 'CONFIRMED_SNAPSHOT' for t in ('1H','4H')}}
        ctx = core.build_context(data, state, {'trades': state.get('trades', [])})
        for key in ('session', 'session_name', 'vwap', 'day_open', 'week_open',
                    'prev_day_high', 'prev_day_low'):
            if key in base:
                ctx[key] = base[key]
        ctx.update(analysis_timeframe=tf, micro_timeframe=micro)
        out[tf] = ctx
    return out


def conditions(setup, ctx, core, anchor=None):
    """Named boolean prerequisites matching each original detector's logic.

    Alternative levels/sides are evaluated as separate branches; report the
    most developed branch. Actual detector output remains authoritative.
    """
    c = [x for x in ctx.get('candles', {}).get('15m', []) if x.confirmed]
    m = [x for x in ctx.get('candles', {}).get('3m', []) if x.confirmed]
    a = max(ctx.get('atr15', 0), 1e-9)
    am = max(ctx.get('atr3', 0), 1e-9)
    price = ctx.get('price', 0)
    sess = ctx.get('session', {})
    struct = ctx.get('structure15', {})
    shift = ctx.get('structure_shift_15m', {})
    zones = ctx.get('zones', {})
    base = zones.get('consolidation', {})
    side = core._trend_side(ctx)
    sign = core.side_sign(side)
    trend = side in ('LONG','SHORT')
    last = c[-1] if c else None
    check = lambda name, ok: {'condition': name, 'met': bool(ok)}
    branches = []

    def branch(items, side=side, level=None):
        branches.append({'conditions': [check(k,v) for k,v in items],
                         'side': core.side_sign(side), 'level': level if level and level > 0 else None})

    if setup == 'SWEEP_RECLAIM':
        low = min((x.low for x in c[-21:-1]), default=0)
        branch([('Є 24 закриті свічки',len(c)>=24),
                ('Знято попередній мінімум',last and last.low<low),
                ('Закриття повернулося над рівень',last and last.close>low>0),
                ('Закриття у верхній частині свічки',last and core._close_location(last)>=core.REACTION_REJECTION_RATIO)], 'LONG',low)
    elif setup == 'CAPITULATION_RECOVERY':
        rows=m[-18:];vol=[x.volume for x in rows if x.volume>0];avg=mean(vol) if vol else 0
        cluster=[x for x in rows if avg and x.volume>=1.9*avg and core._range(x)>=1.6*am]
        low=min((x.low for x in cluster),default=0)
        branch([('Є 20 закритих молодших свічок',len(m)>=20),('Є обсяги у 8 свічках',len(vol)>=8),
                ('Є дві свічки капітуляції',len(cluster)>=2),('Останнє закриття вище низу',m and m[-1].close>low>0)],'LONG',low)
    elif setup == 'RANGE_EDGE_REVERSAL':
        low=base.get('low') or sess.get('day_low',0);high=base.get('high') or sess.get('day_high',0)
        for sd,level in [('LONG',low),('SHORT',high)]:
            branch([('Режим діапазону або переходу',ctx.get('regime') in ('RANGE','TRANSITION')),
                    ('Є 30 закритих свічок',len(c)>=30),('Діапазон ширший за 2 ATR',low>0 and high-low>=2*a),
                    ('Ціна біля межі: до 0.60 ATR',level>0 and abs(price-level)<=.60*a)],sd,level)
    elif setup == 'FAILED_AUCTION_REJECTION':
        vwap=ctx.get('vwap',0)
        refs=[(sess.get('prev_day_high',0),'SHORT'),(sess.get('prev_day_low',0),'LONG'),
              (sess.get('week_high',0),'SHORT'),(sess.get('week_low',0),'LONG'),
              (vwap,'LONG' if last and last.close>vwap else 'SHORT')]
        for level,sd in refs:
            sn=core.side_sign(sd)
            pierced=last and (last.low<level if sn==1 else last.high>level)
            wick=core._lower_wick_ratio(last) if last and sn==1 else core._upper_wick_ratio(last) if last else 0
            branch([('Є 12 закритих свічок',len(c)>=12),('Є опорний рівень',level>0),
                    ('Ціна вийшла за рівень',level>0 and pierced),('Закриття повернулося за рівень',last and sn*(last.close-level)>0),
                    ('Відмова хвостом підтверджена',wick>=core.REACTION_REJECTION_RATIO)],sd,level)
    elif setup == 'LIQUIDITY_SWEEP_REVERSAL_SHORT':
        sw=ctx.get('sweep',{});level=sw.get('level',0)
        branch([('Є закриті свічки',bool(c)),('Зняття ліквідності виявлено',sw.get('sweep')),
                ('Зняття саме над максимумом',sw.get('kind')=='SWEEP_HIGH')],'SHORT',level)
    elif setup == 'BUYER_EXHAUSTION_SHORT':
        bodies=[core._body(x)/a for x in c[-12:]]
        contraction=len(bodies)>=8 and mean(bodies[-4:])<.62*mean(bodies[:4])
        cvd=ctx.get('cvd',{});delta=cvd.get('available') and not cvd.get('rising',True)
        rejection=bool(c) and mean(core._upper_wick_ratio(x) for x in c[-5:])>=.34
        level=struct.get('last_swing_low') or min((x.low for x in c[-4:]),default=0)
        branch([('Є 30 закритих свічок',len(c)>=30),('Перед цим структура LONG',struct.get('direction')=='LONG'),
                ('Тіла свічок стиснулися до 62%',contraction),('Дельта слабшає або верхні хвости',delta or rejection)],'SHORT',level)
    elif setup == 'PULLBACK_CONTINUATION':
        candidates=[]
        if len(c)>=60:
            ladder=core._ema_ladder(c)
            candidates=[(k,v,58) for k,v in list(ladder.items())[:2]]+ [('VWAP',ctx.get('vwap',0),58)]
            for key in ('order_blocks','fvg'):
                candidates += [(z.kind,(z.low+z.high)/2,56+8*z.strength if key=='order_blocks' else 54+6*z.strength)
                               for z in zones.get(key,[]) if z.side==side]
            candidates += [('SWING',v,55) for v in struct.get('swing_lows' if sign==1 else 'swing_highs',[])[-2:]]
        valid=[x for x in candidates if x[1]>0 and sign*(x[1]-price)<=.10*a]
        level=max(valid,key=lambda x:x[2])[1] if valid else None
        branch([('Є напрямок тренду',trend),('Режим допускає відкат',ctx.get('regime') in ('TREND','NORMAL','TRANSITION')),
                ('Є 60 закритих свічок',len(c)>=60),('Є рівень відкату за ціною',bool(valid))],side,level)
    elif setup == 'FRESH_BASE_CONTINUATION':
        impulse=any(core._body(x)>=.9*a and sign*(x.close-x.open)>0 for x in c[-4:])
        level=base.get('low' if sign==1 else 'high',0)
        branch([('Є напрямок тренду',trend),('Є 20 закритих свічок',len(c)>=20),
                ('Сформована база',base.get('found') and base.get('low',0)>0 and base.get('high',0)>0),
                ('Імпульс від бази: 0.9 ATR',impulse)],side,level)
    elif setup in ('ACCEPTANCE_RETEST_CONTINUATION','DIRECTION_FLIP_15M'):
        sd=shift.get('side','NEUTRAL');level=shift.get('level',0)
        age=core.ACCEPTANCE_MAX_AGE_BARS if setup=='ACCEPTANCE_RETEST_CONTINUATION' else 3
        branch([('Пробій структури підтверджений',shift.get('shift')),
                ('Є напрямок і зламаний рівень',sd in ('LONG','SHORT') and level>0),
                ('Зміна структури ще свіжа',shift.get('age_bars',99)<=age)],sd,level)
    elif setup == 'MOMENTUM_NO_PULLBACK_CONTINUATION':
        rows=c[-6:];impulse=[x for x in rows if core._body(x)>=1.25*a and sign*(x.close-x.open)>0]
        gaps=[z for z in zones.get('fvg',[]) if z.side==side and not z.mitigated]
        level=(gaps[-1].high if sign==1 else gaps[-1].low) if gaps else impulse[-1].open if impulse else None
        branch([('Є напрямок тренду',trend),('Є 20 закритих свічок',len(c)>=20),
                ('Є імпульс 1.25 ATR',bool(impulse)),('Рух за 6 свічок не менше 2 ATR',len(rows)>=2 and sign*(rows[-1].close-rows[0].close)>=2*a)],side,level)
    elif setup == 'ACCELERATION_PULLBACK_REENTRY':
        rows=m[-24:];legs=[x for x in rows if core._body(x)>=1.10*am and sign*(x.close-x.open)>0]
        tail=rows[rows.index(legs[len(legs)//2])+1:] if legs else []
        level=(min(x.low for x in tail) if sign==1 else max(x.high for x in tail)) if tail else None
        depth=sign*(legs[len(legs)//2].close-level) if legs and level else 0
        branch([('Є напрямок тренду',trend),('Є 30 молодших і 20 основних свічок',len(m)>=30 and len(c)>=20),
                ('Є дві імпульсні ноги',len(legs)>=2),('Є відкат після першої ноги',bool(tail)),
                ('Глибина відкату 0.30–1.60 ATR',.30*a<=depth<=1.60*a)],side,level)
    elif setup == 'TREND_IGNITION':
        prior=core.detect_consolidation(c[:-1],a) if len(c)>=20 else {}
        for sd,key in [('LONG','high'),('SHORT','low')]:
            level=prior.get(key,0);sn=core.side_sign(sd)
            branch([('Є 20 закритих свічок',len(c)>=20),('База існувала до пробою',prior.get('found')),
                    ('Закриття за межею бази',last and level>0 and sn*(last.close-level)>0),
                    ('Тіло пробою не менше 1.10 ATR',last and core._body(last)>=1.10*a)],sd,level)
    elif setup == 'BREAKOUT_RETEST':
        for key,sd in [('prev_day_high','LONG'),('week_high','LONG'),('prev_day_low','SHORT'),('week_low','SHORT')]:
            level=sess.get(key,0);sn=core.side_sign(sd)
            branch([('Є 20 закритих свічок',len(c)>=20),('Є рівень дня або тижня',level>0),
                    ('Пробій підтверджено закриттям',level>0 and any(sn*(x.close-level)>.10*a for x in c[-6:]))],sd,level)
    elif setup == 'RANGE_COMPRESSION_BREAKOUT':
        sd='LONG' if price>=base.get('mid',0) else 'SHORT';level=base.get('low' if sd=='LONG' else 'high',0)
        branch([('База сформована',base.get('found')),('Стиснення до 1.40 ATR',base.get('compression',99)<=1.4),
                ('Є поточна ціна',price>0)],sd,level)
    elif setup == 'OPENING_RANGE_BREAKOUT':
        low=sess.get('opening_range_low',0);high=sess.get('opening_range_high',0)
        sd='LONG' if price>=(high+low)/2 else 'SHORT';level=high if sd=='LONG' else low
        branch([('Opening range завершено',sess.get('opening_range_complete')),('Є межі opening range',low>0 and high>0),
                ('Ширина не менше 0.50 ATR',high-low>=.5*a)],sd,level)
    elif setup == 'LIQUIDITY_LADDER':
        for key,sd in [('equal_lows','LONG'),('equal_highs','SHORT')]:
            pools=[p for p in ctx.get('liquidity',{}).get(key,[]) if p.get('touches',0)>=2]
            level=min(pools,key=lambda x:abs(x.get('level',0)-price)).get('level',0) if pools else 0
            branch([('Є поточна ціна',price>0),('Є рівень із двома дотиками',bool(pools)),
                    ('Відстань до рівня до 2.50 ATR',level>0 and abs(price-level)<=2.5*a)],sd,level)
    elif setup in ('FAILED_OPENING_RANGE_BREAKOUT','OR_FAILURE_2_SHORT'):
        refs=[(sess.get('opening_range_high',0),'SHORT')]
        if setup=='FAILED_OPENING_RANGE_BREAKOUT': refs.append((sess.get('opening_range_low',0),'LONG'))
        rows=c[-10:] if setup=='OR_FAILURE_2_SHORT' else c[-6:]
        for level,sd in refs:
            pierced=[x for x in rows if x.high>level] if sd=='SHORT' else [x for x in rows if x.low<level]
            refused=[x for x in pierced if core.side_sign(sd)*(x.close-level)>0]
            branch([('Opening range завершено',sess.get('opening_range_complete')),('Є рівень і свічки',level>0 and len(c)>=(12 if setup=='OR_FAILURE_2_SHORT' else 8)),
                    ('Є потрібна кількість пробоїв',len(pierced)>=(2 if setup=='OR_FAILURE_2_SHORT' else 1)),
                    ('Усі спроби закрилися всередині',bool(pierced) and len(refused)==len(pierced))],sd,level)
    elif setup == 'FAILED_BREAKOUT_SHORT':
        level=max((x.high for x in c[-21:-1]),default=0)
        branch([('Є 24 закриті свічки',len(c)>=24),('Знято попередній максимум',last and last.high>level>0),
                ('Закриття під рівнем',last and last.close<level),
                ('Підтверджена відмова від максимуму',last and (core._upper_wick_ratio(last)>=core.REACTION_REJECTION_RATIO or core._close_location(last)<=.45))],'SHORT',level)
    elif setup == 'MSS_REVERSAL_SHORT':
        level=shift.get('level') or max((x.high for x in c[-6:]),default=0)
        branch([('Є 30 закритих свічок',len(c)>=30),('MSS спрямований вниз',shift.get('side')=='SHORT'),
                ('Ціна нижче початку попередньої ноги',len(c)>=20 and c[-1].close<min(x.close for x in c[-20:-14])),
                ('Є рівень ретесту',level>0)],'SHORT',level)
    elif setup in ('SESSION_MEAN_RECLAIM','DAILY_WEEKLY_OPEN_RECLAIM'):
        refs=[ctx.get('vwap',0)] if setup=='SESSION_MEAN_RECLAIM' else [sess.get('day_open',0),sess.get('week_open',0)]
        for level in refs:
            sn=1 if price>=level else -1;sd='LONG' if sn==1 else 'SHORT'
            displaced=any(sn*(x.close-level)>1.10*a for x in c[-10:-1]) if setup=='SESSION_MEAN_RECLAIM' else any(sn*(x.close-level)<-.35*a for x in c[-10:-1])
            branch([('Є 12 закритих свічок',len(c)>=12),('Є опорний рівень вартості',level>0),
                    ('Є попередній відхід від рівня',displaced)],sd,level)
    elif setup == 'TIME_OF_DAY_ADAPTIVE':
        low=sess.get('asia_low',0);high=sess.get('asia_high',0)
        for sd,level in [('LONG',low),('SHORT',high)]:
            branch([('Сесія Лондона або Нью-Йорка',ctx.get('session_name') in ('LONDON_OPEN','LONDON','NY_OVERLAP','NY_PM')),
                    ('Азійський діапазон завершено',sess.get('asia_complete')),('Діапазон ширший за 0.80 ATR',low>0 and high-low>=.8*a),
                    ('Ціна біля межі: до 0.75 ATR',level>0 and abs(price-level)<=.75*a)],sd,level)
    else:
        raise ValueError('Missing setup conditions: '+setup)

    if anchor:
        # A detector has confirmed its OWN branch, which may not be the closest.
        selected=min(branches,key=lambda b:(b['side']!=core.side_sign(anchor.side),
                     abs((b['level'] or anchor.level)-anchor.level)))
        selected.update(side=core.side_sign(anchor.side),level=anchor.level)
        # Detector output remains authoritative if a display prerequisite differs.
        ready=100.;status='DETECTED'
    else:
        selected=max(branches,key=lambda b:(sum(x['met'] for x in b['conditions'])/len(b['conditions']),
                           -abs(price-(b['level'] or price))))
        ready=round(100*sum(x['met'] for x in selected['conditions'])/len(selected['conditions']),1)
        status='FORMING' if ready>0 else 'NO_SETUP'
        # Never report fully ready when the authoritative detector returned None.
        if ready==100:
            selected['conditions'].append(check('Оригінальний детектор підтвердив сетап',False))
            ready=round(100*(len(selected['conditions'])-1)/len(selected['conditions']),1)
    selected.update(setup=setup,readiness_pct=ready,status=status,
                    met_conditions=sum(x['met'] for x in selected['conditions']),
                    total_conditions=len(selected['conditions']),
                    missing_conditions=[x['condition'] for x in selected['conditions'] if not x['met']])
    return selected


def proximity(price, entry, initial=None):
    if not entry or entry<=0 or price<=0:
        return {'distance_pct':None,'approach_pct':None}
    distance=abs(price-entry)
    initial_distance=abs(initial-entry) if initial is not None else None
    approach=max(0.,min(100.,100*(1-distance/initial_distance))) if initial_distance and initial_distance>0 else None
    return {'distance_pct':100*distance/price,'approach_pct':approach}

# Exact original registry order, including the historic 15M enum identifier.
SETUP_NAMES = (
    'SWEEP_RECLAIM','CAPITULATION_RECOVERY','RANGE_EDGE_REVERSAL',
    'FAILED_AUCTION_REJECTION','LIQUIDITY_SWEEP_REVERSAL_SHORT','BUYER_EXHAUSTION_SHORT',
    'PULLBACK_CONTINUATION','FRESH_BASE_CONTINUATION','ACCEPTANCE_RETEST_CONTINUATION',
    'MOMENTUM_NO_PULLBACK_CONTINUATION','ACCELERATION_PULLBACK_REENTRY','DIRECTION_FLIP_15M',
    'TREND_IGNITION','BREAKOUT_RETEST','RANGE_COMPRESSION_BREAKOUT','OPENING_RANGE_BREAKOUT',
    'LIQUIDITY_LADDER','FAILED_OPENING_RANGE_BREAKOUT','FAILED_BREAKOUT_SHORT','MSS_REVERSAL_SHORT',
    'OR_FAILURE_2_SHORT','SESSION_MEAN_RECLAIM','DAILY_WEEKLY_OPEN_RECLAIM','TIME_OF_DAY_ADAPTIVE',
)
