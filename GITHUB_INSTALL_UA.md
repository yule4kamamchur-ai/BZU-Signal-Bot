# Встановлення v12.5.0

1. Збережіть актуальні last_signal_v6_4.json і signal_journal_v6_4.json у репозиторії. Під час оновлення не замінюйте їх старими копіями із завантажених архівів.
2. Оновіть код і тести з архіву, зокрема НОВІ limit_structure.py, market_archive.py, test_v12_5_early_limits.py і папку testdata. bot_oneshot.py, full_limit_engine.py, signal_quality.py, setup_scanner.py та research_validate.py оновлюються разом.
3. ОБОВ’ЯЗКОВО оновіть .github/workflows/main.yml. Папка .github прихована на Mac. Для зручності копія нового workflow лежить у видимому WORKFLOW_main.yml: вставте весь її текст у файл .github/workflows/main.yml через редактор GitHub. У workflow має бути v12.5.0, два cron: */15 та 5,10,20,25,35,40,50,55. Старий workflow із одним cron залишить лише 15-хвилинне сканування.
4. Залиште один активний workflow бота. Запустіть Actions → Run workflow. Тести та перевірка сумісності мають пройти.
5. У заголовку нового Telegram-звіту має бути v12.5.0. market_history_v12.json створиться автоматично і зберігатиметься разом зі станом та журналом. Події тихих 5-хвилинних сканів увійдуть у наступний 15-хвилинний звіт.

Архів оновлення містить код і контрольні приклади. Поточні робочі файли стану/журналу не включені, щоб їх не перезаписати. Оновлюйте файли в наявному репозиторії; не видаляйте весь старий репозиторій чи папку разом із його станом.

Перевірки локально:

```bash
python -m py_compile bot_oneshot.py full_limit_engine.py signal_quality.py setup_scanner.py limit_structure.py market_archive.py price_forecast.py research_validate.py
python bot_oneshot.py --self-test
ABS_MIN_STOP_PCT=0.004 python bot_oneshot.py --legacy-self-test
```
