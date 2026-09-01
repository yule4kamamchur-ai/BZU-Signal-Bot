# BZU Signal Bot v9.5.59 — Canonical Execution Core

## Результат

Канонічний execution-контур відокремлено від історичного release-stack. Жива
торгова поведінка v9.5.58 збережена, але Router, Final Authority, Risk Ledger і
signal journal compaction тепер мають по одній явній runtime-точці входу.

Нові модулі:

- `bzu_core/execution_engine.py` — один `CanonicalExecutionEngine`;
- `bzu_core/forward_control.py` — forward-аналітика фактичних закритих угод;
- `bzu_core/setup_families.py` — шість сімейств і cross-setup episode dedup;
- `bzu_core/__init__.py` — стабільний public contract пакета.

Історичні функції v9.5.x не видалені всліпу: вони перейменовані в унікальні
`core/compat` callbacks для регресій. Однакових top-level `def` для Router,
Final Authority та signal compaction більше немає. Public compatibility names
прив'язуються один раз до v9.5.59.

## Єдині runtime entry points

| Контур | Канонічна функція |
|---|---|
| Router | `canonical_execution_router_v9559` |
| Final staged contract | `canonical_final_execution_contract_v9559` |
| Trade/no-trade authority | `apply_canonical_execution_authority_v9559` |
| Risk Ledger | `canonical_risk_ledger_v9559` |
| Signal compaction | `compact_signal_canonical_v9559` |

`CanonicalExecutionEngine` записує trace кожного рішення: один Router read,
один profile build, один final risk authority pass і один фінальний action
mutator. Router не отримав trade authority.

## Forward control

Для `PREMIUM_FULL`, `STANDARD_ENTRY` та `EARLY_PROBE` автоматично рахуються:

- кількість фактично закритих угод;
- wins/losses/breakeven і win rate;
- expectancy та net result у R;
- gross profit/loss і profit factor;
- середні MFE та MAE;
- adverse runtime slippage у R;
- maturity статус вибірки.

Старі 11 угод віднесені до `historical_actual_baseline`. Вони не можуть
підвищувати довіру до v9.5.59. Справжній forward cohort включає тільки угоди,
відкриті версією v9.5.59, і зараз має нуль спостережень. Автоматична зміна
live-порогів статистикою вимкнена.

Поточний історичний baseline:

- 11 закритих PROBE;
- 3 wins, 7 losses, 1 breakeven;
- expectancy `−0.022218R`;
- net `−0.244393R`;
- profit factor `0.894611`;
- середній MFE `0.359273R`;
- середній MAE `0.335545R`.

Slippage зараз означає різницю між requested signal entry та записаною runtime
entry, нормовану на initial R. Реального broker-fill slippage бот не може знати,
доки не буде підключене фактичне виконання ордера.

## Шість канонічних сімейств

1. `LIQUIDITY_REVERSAL` — 6 setup;
2. `TREND_CONTINUATION` — 5 setup;
3. `STRUCTURAL_EXPANSION` — 4 setup;
4. `SESSION_EXPANSION` — 2 setup;
5. `FAILED_EXPANSION` — 4 setup;
6. `VALUE_RECLAIM` — 3 setup.

Разом: 24/24. Перед Executive selection одночасні гіпотези групуються за
family + side + ATR-normalized anchor + evidence-hour. У кожному market episode
залишається кандидат із найвищим evidence-adjusted selection score; пригнічені
назви зберігаються в audit, а не зникають без пояснення.

## Перевірки

- повний історичний self-test chain — PASS;
- v9.5.58 ordinary-entry tests — 9/9 PASS;
- v9.5.59 canonical-core tests — 9/9 PASS;
- setup family coverage — 24/24 у 6 сімействах;
- runtime configuration — valid, errors `[]`;
- architecture/authority audit — `READY`;
- Python compile — PASS;
- workflow YAML — valid;
- forward snapshot persistence — PASS;
- AST у канонічному execution-контурі: кожна нова entry-point функція оголошена
  рівно один раз; `pass` nodes і constant-condition dead branches — 0.

## Чесне архітектурне обмеження

`bot_oneshot.py` все ще містить великий compatibility-корпус детекторів,
міграцій та історичних тестів. У цій версії очищено саме канонічний execution
контур, forward control і family identity — без ризикового переписування 24
детекторів за один реліз. Наступне безпечне архітектурне розділення має
переносити detector families і tests по одному модулю зі snapshot-parity
перевіркою, не змінюючи сигнали одночасно з refactor.
