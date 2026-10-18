# Встановлення BZU v12.6

Оновлення активної версії: `full-ict-v12.6.0-lifecycle-adaptive-policy`. Передає всі 24 сетапи та збережений стан у нову політику без WR-фільтра.

## Файли для GitHub

Додати:

- `lifecycle_forecast.py`
- `trade_dependencies.py`
- `research_evidence.py`
- `research_history.py`
- `research_v12_6.py`
- `test_v12_6_policy.py`

Замінити:

- `bot_oneshot.py`
- `full_limit_engine.py`
- `signal_quality.py`
- `setup_scanner.py`
- `limit_structure.py`
- `notification_report.py`
- `research_validate.py`
- `analyze_bot.py`
- `test_full_limit_engine.py`
- `test_signal_quality.py`
- `test_v12_4_2_reports.py`
- `test_v12_4_scanner.py`
- `test_v12_5_early_limits.py`
- `.github/workflows/main.yml`
- `WORKFLOW_main.yml` — його дзеркальна копія; виконується файл із `.github/workflows/`.

Для репозиторію, який ще не має v12.5.1, завантажте також його відсутні модулі й тести з цього повного комплекту: `notification_report.py`, `analyze_bot.py`, `test_notification_report.py`, `market_archive.py` та решту тестових файлів. Повний список відмінностей від першого наданого ZIP є у `CHANGED_FILES_v12_6.txt`.

**Не замінюйте новіший робочий стан архівними копіями:** `last_signal_v6_4.json`, `signal_journal_v6_4.json`, `market_history_v12.json`. У комплекті це джерела станом на 18.10 о 21:34 за Києвом. Не перезаписуйте також уже заповнений `research_market_v12_6.json` порожнім стартовим файлом із комплекту.

## Запуск

1. Завантажити кодові файли одним комітом у default branch. Особливо важливо разом замінити launcher, engine і workflow.
2. Залишити поточні state/journal і secrets Telegram. Workflow використовує Ubuntu 24.04, перевіряє комплектність та запускає всі тести.
3. Запустити Actions → Run workflow. У сповіщенні має бути v12.6.0. Чинні ордери переходять зі своїми початковими умовами.
4. Новий чистий архів створиться автоматично після отримання підтверджених даних live-колектором. Порожній JSON із комплекту завантажувати необов’язково.

Звіт залишається кожні 15 хвилин за cron; між звітами передбачені тихі скани. Спосіб хостингу не змінений.

Для нових планів `INCLUDE_COMMISSION=true`. Ставки задаються `MAKER_FEE_RATE` і `TAKER_FEE_RATE`; початкові значення 0,0002 / 0,0005. Старі чинні плани зберігають свою початкову угоду про витрати. Бюджет фандингу в моделі — окреме припущення, а не підтверджена плата біржі.

## Перевірка коду

```bash
python bot_oneshot.py --self-test
```

Очікуваний результат цього комплекту: **298 тестів, OK**. Self-test ізолює обидва архівні шляхи від робочих даних.

## Чисте дослідження

За спостереженими даними:

```bash
python research_v12_6.py --archive research_market_v12_6.json --output research_v12_6_output
```

Для окремого контрольованого завантаження історії з OKX:

```bash
python research_v12_6.py --download --days 90 --instrument BZ-USDT-SWAP --output research_v12_6_history
```

Другий варіант завантажує історію й зберігає відповіді джерела; може потребувати значного часу. Його котирування для моделювання реконструюються з явним припущенням про спред. Це відокремлено від справжніх live-котирувань. У цьому сеансі 90-денну історію не завантажували.

Результати: `validation.json`, окремі підсумки TF, оцінка прогнозів і, після достатнього часового покриття, навчальний артефакт `quality_model.json`. За порожнього або короткого архіву звіт прямо повідомляє про відсутність достатнього періоду. Це не обмеження на торгові входи й не очікування 40 угод.

Пояснення нової політики — `V12_6_IMPLEMENTATION_UA.md`; приклад короткого сповіщення — `MESSAGE_EXAMPLE_v12_6_UA.txt`. Новий WR можна виміряти тільки за наступними угодами або окремим чистим відкладеним дослідженням.
