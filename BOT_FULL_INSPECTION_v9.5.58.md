# Глобальний аудит звичайних входів BZU Signal Bot v9.5.58

Дата: 2026-09-01  
Об’єкт: production-код, усі 24 setup contracts, Final Authority, Router, risk ledger, workflow та журнал з 407 сигналів / 11 закритих угод.

## Головний діагноз

Гілка звичайного входу була не «рідкісною», а фактично недосяжною:

- у 407 сигналах: 397 `NO_SETUP`, 10 `PROBE_ENTRY`, 0 `ENTRY`, 0 `RISKY_ENTRY`;
- усі 11 фактичних угод відкрито як `PROBE`;
- 74 сигнали вже мали score ≥75, setup/timing/trade quality ≥70, fresh trigger, supported entry та execution-ready plan, але 68 з них залишилися `NO_SETUP`;
- Router у всьому журналі вибрав `FIRST_RETEST` 182 рази, `ONE_3M_CONFIRM` 116, `MARKET_NOW` 29 і `NONE` 80;
- у всіх 33 останніх рішеннях v9.5.56 Router дав тільки `FIRST_RETEST` (20) або `ONE_3M_CONFIRM` (13). Жодного маршруту, який міг дати старий `FULL_ENTRY`, не було.

## Три незалежні причини мертвого normal-entry path

1. **Недосяжний CORE.** Staged Executive дозволяв `CORE` лише за `statistical_status=PROVEN_POSITIVE`. До зрілої вибірки calibration повертав `ACCEPT_STAGED`, який завжди обмежував stage до `PROBE`. Тобто бот вимагав статистику normal-entry ще до того, як міг накопичити normal-entry sample.
2. **Другий Router-veto.** Навіть якщо setup/trigger/plan/risk були готові, `FIRST_RETEST` або pending `ONE_3M_CONFIRM` закривали двері до `FULL_ENTRY`. Router фактично став другим trade/no-trade authority, хоча мав лише обирати fill tactic.
3. **Повторне стискання risk.** Навіть після умовного допуску старий ledger повторно перемножував entry quality, HTF, liquidity, structure і bootstrap ML, хоча ці факти вже були в допуску. У 11 історичних угодах risk становив лише 0.005–0.0225% за normal risk 0.50%.

## Нова структура v9.5.58

Final Authority тепер має чотири виходи:

1. **PREMIUM_FULL** — звичайний `ENTRY`, `CORE`, до 100% дозволеного normal risk. Вимагає causal execution proof, strong anchor, преміальної якості, низького ASI та сильного runway.
2. **STANDARD_ENTRY** — звичайний `ENTRY`, `CORE`, номінально 55–75% normal risk. За default normal risk 0.50% це 0.275–0.375% до абсолютних capital/geometry/empirical caps.
3. **EARLY_PROBE** — зберігає професійну v9.5.57 логіку: 25–35% normal risk для якісного native probe, 15–20% для structural-runway scout, окремий cap для recovery.
4. **WAIT** — тільки коли немає валідної execution structure, fresh trigger, execution-ready plan, valid stop geometry, економічного runway або є factual invalidation/capital block.

## Evidence quorum для STANDARD_ENTRY

Звичайний вхід не виникає від одного score. Одночасно потрібні:

- score ≥75, setup quality ≥72, timing ≥72, trade quality ≥74;
- structural control ≥58, ASI ≤42, setup-relative percentile ≥0.60;
- state machine=`EXECUTION`;
- fresh trigger, supported revalidation, execution-ready valid plan і correct stop side;
- current setup-native retest або прямий live execution source;
- regime direction не суперечить side, Location/Structure не `AGAINST`, Statistical не негативний;
- primary runway ≥0.08R; micro-target нижче цього порогу без достатньої економіки не може стати normal entry;
- відсутні factual hard blockers і directional market guard.

`ONE_3M_CONFIRM`, `FIRST_RETEST`, weak non-invalidated anchor і immature calibration можуть обмежити розмір або залишити лише `EARLY_PROBE`, але більше не є абсолютним Router/calibration veto.

## Виправлення risk cascade

Для `STANDARD_ENTRY` новий final sizing:

- перебудовує ledger на фактично дозволеному `CORE`, а не на старому provisional `PROBE`;
- не перемножує повторно ті самі quality-докази, які вже пройшли evidence quorum;
- залишає bounded confidence correction 90–100% для standard і 85–100% для premium;
- безумовно зберігає daily capital cap, empirical setup/source caps, short-reversal caps, geometry і factual invalidation.

У regression-fixture номінальна standard fraction 61.43% дала реальний final risk 0.27989% за normal risk 0.50%, замість старого 0.0562% після дубльованого confidence cascade.

## Cadence replay без forced trades

На compact-журналі v9.5.58 policy знайшла:

- 11 replayable standard-entry rows;
- 8 незалежних thesis-епізодів;
- за active day: `1, 1, 2, 1, 2, 1`;
- середнє: 1.333 якісного normal-entry opportunity на active day замість історичних 0.

Цільовий діапазон 1–3 на active day є аудитом досяжності, не quota. Код містить `V9558_CADENCE_QUOTA_ENABLED=False`; якщо evidence quorum немає, бот не має права створити угоду заради денної кількості.

## Методологічне обмеження

Історична вибірка має лише 11 закритих угод: 3 win, 7 loss, 1 flat, сума −0.244393R. Це недостатньо для чесної оптимізації win rate. Тому пороги v9.5.58 не підганялися під realized outcomes; replay вимірює лише reachability/cadence. Наступний етап після накопичення даних — chronological forward/OOS review, а не повторне ручне підкручування порогів.

Це узгоджується з проблемою backtest selection bias і multiple testing, описаною в первинних роботах Bailey et al.:

- https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551

## Перевірки релізу

- Python compilation — PASS;
- повний успадкований regression suite — PASS;
- v9.5.57 balanced-authority scenarios — 9/9 PASS;
- v9.5.58 ordinary-entry scenarios — 9/9 PASS;
- runtime configuration — `valid=true`, errors=[];
- architecture audit — PASS, authority=`READY`;
- усі 24 setup types є в `SETUP_STATE_MACHINE_REGISTRY`, missing=[];
- journal replay — PASS, блок `v9558_ordinary_entry_reachability` сформований;
- статичний AST-перегляд: 0 `pass`, 0 constant-frozen `if`, 0 простих unreachable statements; нові authority wrappers мають явний versioned base alias.

## Чого реліз не обіцяє

v9.5.58 повертає реальну досяжність якісних звичайних входів і прибирає мертві переходи, але не може гарантувати «максимальний win rate» або дві угоди щодня незалежно від ринку. Професійна мета — позитивна expectancy за контрольованого drawdown, а не штучна кількість або підігнана історична точність.
