# Повна інспекція BZU Signal Bot v9.5.57

Дата перевірки: 2026-09-01  
Об’єкт: production-код, workflow, state/journal contract і журнал після v9.5.56.

## Діагноз журналу після оновлення

Бот не був зламаний на рівні пошуку сетапів. У 33 сигналах v9.5.56 він стабільно знаходив LONG-кандидатів, усі 33 мали готовий trigger, 30 мали підтриманий вхід, але фінальний результат був 32 `NO_SETUP` і лише один `PROBE_ENTRY`.

Основні причини:

- 13 сетапів потрапили в `ONE_3M_CONFIRM / DEFERRED`; для частини з них уже існував свіжий `ZONE_RETEST_PROBE`, але фінальна authority все одно вимагала ще одну post-decision 3M свічку;
- 20 сетапів маршрутизувалися через `FIRST_RETEST`; серед 11 готових планів 10 мали nearest runway нижче 0.08R;
- єдиний probe закрився з −0.0242R через `NO_FOLLOWTHROUGH_EXIT`, після чого ціна відновила ріст; без нового якісного допуску повторний ранній вхід не відбувся;
- у чотирьох `NO_SETUP` рядках журнал помилково показував `blocking_reason: NONE`.

Anchor не був першопричиною: у нових рядках схема сформована, а міграцій порожньої generic-схеми не зафіксовано. ML preconfirmation також не блокував входи.

## Реалізоване рішення

1. **Преміальний native-probe contract.** `ONE_3M_CONFIRM` залишається обов’язковим для `FULL_ENTRY`. Для `EARLY_PROBE` його може замінити лише реальний setup-native retest за одночасного виконання всіх умов: score ≥78, setup ≥72, timing ≥72, trade quality ≥74, structural score ≥60, ASI ≤40, setup-relative percentile ≥0.70, state machine=`EXECUTION`, свіжий trigger, готовий plan, валідна stop geometry, підтримана revalidation і відсутність factual invalidation.
2. **Структурний runway ladder.** Бот бачить не лише найближчу ціль, а до шести наступних незібраних цілей. Внутрішній 15M micro-target може бути checkpoint, якщо за ним є кваліфікована 1H/4H/1D або зовнішня liquidity target щонайменше за 0.75R. Такий виняток дозволяє тільки `SCOUT_PROBE`, ніколи full entry.
3. **Ризик без подвійного стискання.** Стандартний якісний native probe використовує 25–35% normal risk. Runway обирає одну частку всередині цього діапазону і більше не множить її повторно на окремий runway factor. Risk ledger, денний cap і менший уже запланований ризик залишаються верхньою межею.
4. **Окремий scout-ризик.** Якщо найближча ціль є micro-noise, але structural runway достатній, розмір обмежено 15–20% normal risk.
5. **Безпечний re-entry.** Після `NO_FOLLOWTHROUGH_EXIT` дозволено максимум один recovery probe по тому самому thesis і лише за новішої confirmed 3M свічки; його ризик не перевищує 20% normal risk. Повторний churn блокується.
6. **Точна телеметрія.** Після останнього mutator журнал зберігає `final_execution_tier_v9557`, точний список фінальних blocker-ів і більше не замінює реальну причину очікування на `NONE`.

## Перевірка на журналі

Audit журналу після v9.5.56 знайшов:

- 33 рядки v9.5.56;
- 13 очікувань `ONE_3M_CONFIRM`;
- 3 історичні сигнали, які повністю проходять новий суворий native-probe replay: `7273651683`, `4f63fccf53`, `fea419778a`;
- 4 рядки `WAIT` із некоректним `blocking_reason: NONE`, що виправлено для нових сигналів.

Replay є діагностикою доступності, а не симуляцією прибутку: compact journal не містить повного майбутнього candle path для чесного перерахунку результату кожної пропущеної угоди.

## Інваріанти безпеки

- `FULL_ENTRY` не послаблено;
- invalidation, bad stop geometry, неготовий plan, stale/unsupported trigger і реальна відсутність runway залишаються `WAIT`;
- native retest не може підняти позицію до full size;
- усі 24 named setup contracts залишаються зареєстрованими;
- одна активна позиція, RR floors, daily risk cap, 15-хвилинний scheduler і causal Router lifecycle збережені;
- гарантія майбутнього win rate неможлива; алгоритм оптимізує якість входу та expectancy під контрольованим ризиком.

## Перевірки релізу

- Python compilation — пройшла;
- повний вбудований regression suite — пройшов;
- v9.5.57 balanced-authority scenarios — 9/9;
- offline journal audit — пройшов, блок `v9557_balanced_native_probe_authority` сформований;
- release/version/workflow seal — синхронізований на v9.5.57.
