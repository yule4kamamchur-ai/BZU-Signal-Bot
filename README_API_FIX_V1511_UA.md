# BZU MULTI v15.1.1 — API FIX

## Причина
У наданому стані: останній успішний аналіз 26.11.2026 21:20 (Київ), спроба 21:25:59, FAILED_DATA, 79 помилок і 0/7 свіжих котирувань. DETAIL, усі kline та ticker повертали HTTP 403. Старий REST https://contract.mexc.com офіційно замінений на https://api.mexc.com. Оголошення: https://www.mexc.com/announcements/article/futures-api-access-domain-update-17827791532974 ; документація: https://www.mexc.com/api-docs/futures/integration-guide . Зміна домену усуває застарілу конфігурацію, але не гарантує усунення інших причин HTTP 403.

## Встановлення
Розпакувати патч у корінь чинного репозиторію, включно з прихованим шляхом .github/workflows/main.yml. Не замінювати multi_scanner_memory.json, multi_scanner_state.json або старі журнали. Зберегти наявний cron-job.org: URL workflow main.yml і workflow_dispatch не змінилися; внутрішній schedule не повертаємо. Запустити workflow один раз вручну або перевірити наступний зовнішній запуск. Новий workflow називається BZU MULTI v15.1.1 — MEXC scanner.

У Summary кроку scan перевірити API, fresh quotes, analysed, HTTP errors. Успішне відновлення: свіжий updated_ts, ideally 7/7 quotes та 7/7 analysed, status OK. DEGRADED означає часткові дані. FAILED_DATA зберігає попередній час успішного аналізу й публікує нову діагностику; після публікації workflow позначає помилку. Дві відмови доступу на різних публічних endpoint зупиняють решту запитів у цьому циклі. Жодних обходів обмежень доступу. Якщо новий домен також повертає 403, перевірити права/доступність через підтримку MEXC; не створювати біржові торгові ключі для цього публічного сканера.

Зовнішній інтервал фактично задається cron-job.org. Якщо там 3 хвилини, додати repository variable MEXC_SCAN_INTERVAL_SEC=180 (Settings → Secrets and variables → Actions → Variables). Якщо 5 хвилин, залишити дефолт 300. Це лише параметр очікуваного інтервалу для діагностики, не планувальник.

Стратегія, геометрія, модель та облік позицій не змінені. WS ціни не є новим серверним аналізом. Після відмови API відкриті PAPER позиції можуть мати DATA_GAP; пропущений ринковий рух не можна вважати перевіреним. Реальні SL/TP повинні бути на біржі.

## Тести
python -m unittest test_mexc_multi_scanner test_mexc_hybrid_v15 test_mexc_structural_v151 test_mexc_api_v1511
node test_mexc_dashboard_v151.cjs
42 Python-тести та 4 перевірки JS пройшли локально; відтворення 403 на копії поточного стану зберегло обидві активні позиції, їх SL/TP та курсори. YAML перевірено парсером. Живий запит з локального середовища не виконано через відсутність DNS; перевірка доступності на GitHub потрібна після встановлення. Не змінювали live WebSocket, оскільки на скриншоті котирування надходять.
