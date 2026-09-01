# Повна інспекція BZU Signal Bot v9.5.56

Дата перевірки: 2026-08-31  
Об’єкт: `bot_oneshot.py`, production-конфігурація та журнал `signal_journal_v6_4.json`

## Висновок

Бот не має ознак повної поломки пошуку сигналів. Основний дефект був у надмірно жорсткому фінальному допуску до виконання: порожня provenance-схема anchor могла виглядати як абсолютний veto, а advisory-сигнали змішувалися з фактичними блокерами.

У версії v9.5.56 фінальний execution authority переведено на три рівні:

- `FULL_ENTRY` — повний розмір лише за сильною структурою, свіжим trigger, готовим планом, валідним ризиком і сильним невідхиленим anchor;
- `EARLY_PROBE` — 25–40% планового ризику для якісного раннього входу, коли структура/trigger/plan/risk валідні, але anchor або advisory-якість ще не ідеальні;
- `WAIT` — відсутня структура, є фактична invalidation, помилка даних/ризику або не виконана причинна 3m-підтверджувальна умова.

## Що виправлено

1. **Anchor provenance.** Для всіх setup-типів production opportunity тепер зберігається `execution_anchor_schema`. Порожня generic-схема мігрується до `EXECUTION_ANCHOR_RECONSTRUCT_ON_REENTRY_V9_5_56`; спеціальна provenance для `FAILED_AUCTION_REJECTION` залишається fail-closed, якщо її немає.
2. **FINAL_EXECUTION_AUTHORITY.** `FIRST_RETEST`, слабкий anchor, advisory score, calibration і preconfirmation більше не є безумовним veto. Вони впливають на tier та розмір позиції. Фактичні блокери обмежені класами `DATA`, `RISK`, `INVALIDATION`.
3. **Ранні входи.** Додано причинно-безпечний `EARLY_PROBE` із cap 25–40% від normal risk; ризик не може зрости вище початкового плану. `ONE_3M_CONFIRM` збережено як реальну causal-вимогу.
4. **Мертвий/затінений код.** Видалено 21 неаліасоване top-level перевизначення (741 рядок недосяжної історичної логіки). Залишені дублікати мають явні `_base`/`_effective` аліаси й використовуються як сумісні шари.
5. **Тихі no-op та помилки.** HTTP-помилки більше не ковтаються через `except Exception: pass`; вони фіксуються в `last_error`. Прибрано порожній validator no-op і порожній клас-виключення.
6. **Release seal.** Версію, architecture version, state validators, CLI та GitHub workflow синхронізовано на v9.5.56. Залежність `requests` зроблена сумісною з актуальним runtime (`requests>=2.31.0`).

## Перевірки

- Python-компіляція: пройшла.
- Вбудований self-test: `SELF-TEST PASSED`; v9.5.55 — 8/8, v9.5.56 — 8/8.
- Runtime configuration validator: valid; version `pro-hybrid-confluence-v9.5.56-three-level-execution-authority`.
- Контракт усіх 24 setup-типів: 24/24 зареєстровані, мають семантику та family map; валідний fixture дає `FULL_ENTRY` для 24/24.
- Покриті типи: `SWEEP_RECLAIM`, `CAPITULATION_RECOVERY`, `DIRECTION_FLIP_15M`, `TREND_IGNITION`, `PULLBACK_CONTINUATION`, `FRESH_BASE_CONTINUATION`, `BREAKOUT_RETEST`, `RANGE_COMPRESSION_BREAKOUT`, `RANGE_EDGE_REVERSAL`, `ACCEPTANCE_RETEST_CONTINUATION`, `MOMENTUM_NO_PULLBACK_CONTINUATION`, `ACCELERATION_PULLBACK_REENTRY`, `SESSION_MEAN_RECLAIM`, `OPENING_RANGE_BREAKOUT`, `FAILED_OPENING_RANGE_BREAKOUT`, `DAILY_WEEKLY_OPEN_RECLAIM`, `LIQUIDITY_LADDER`, `FAILED_AUCTION_REJECTION`, `TIME_OF_DAY_ADAPTIVE`, `LIQUIDITY_SWEEP_REVERSAL_SHORT`, `FAILED_BREAKOUT_SHORT`, `MSS_REVERSAL_SHORT`, `BUYER_EXHAUSTION_SHORT`, `OR_FAILURE_2_SHORT`.
- AST/static scan: 0 неаліасованих shadowed top-level функцій.
- Control-flow scan: 0 простих недосяжних операторів після `return/raise/break/continue`, 0 константно заморожених умов.
- Пошук явних `pass`, `TODO`, `FIXME`, `NotImplemented`: збігів немає.
- Journal audit: успішно сформований звіт із блоком `v9556_three_level_execution_authority`.

## Дані журналу та обмеження висновків

Історичний journal містить 250 запусків execution-anchor аудиту: 76 заблокованих кандидатів, 73 `WAIT_RETEST`, 11 відновлених кандидатів і 51.61118R сумарного оціненого missed move. Це підтверджує проблему надмірного veto, але не є гарантією майбутнього win rate.

У старому журналі немає фактичних сигналів для `CAPITULATION_RECOVERY`, `RANGE_COMPRESSION_BREAKOUT`, `ACCELERATION_PULLBACK_REENTRY` і `FAILED_AUCTION_REJECTION`. Runtime-контракт для них активний; потрібні нові forward-спостереження для статистичної оцінки. Невелика стара вибірка `FIRST_RETEST` (7 закритих угод, 2 wins, 28.6%, −1.8616R) є лише описовою й не використовується як прогноз.

Гарантувати максимальний майбутній win rate неможливо. Поточна політика оптимізує expectancy: зберігає жорсткі блоки для реального ризику/інвалідації, але повертає контрольовані ранні probes без збільшення запланованого ризику.

## Як повторити аудит

```bash
PYTHONPYCACHEPREFIX=/tmp/bzu_pycache python3 BZU-Signal-Bot-main/bot_oneshot.py --self-test
PYTHONPYCACHEPREFIX=/tmp/bzu_pycache python3 BZU-Signal-Bot-main/bot_oneshot.py --audit-journal BZU-Signal-Bot-main/signal_journal_v6_4.json
```
