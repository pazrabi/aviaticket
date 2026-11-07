# КТ1. Шесть веток от `367f108` и пробная `kt1-all`

**Задание:** `docs/review/kt1.md` (приёмщик, 2026-11-07, день) с дополнением 2 к корректировке 7 (11:30, пп. 5–8).
**База:** `upstream/main` на старте — `367f108398b2877c91b219b1bd6451205ce20049`. Все ветки — от неё, каждая в своём worktree `~/avia-scale/kt1-<тема>`. Только локально: без push, PR и влития в `main` и `kt0-upstream`.
**Пробная ветка:** `kt1-all` (`~/avia-scale/kt1-all`) — все шесть веток вместе, тоже от `367f108`. Нужна, чтобы проверить влитие и полный pytest; в `main` не вливается. Этот отчёт лежит в ней отдельным коммитом.

## Итог

| Ветка | Коммиты | `diff --stat 367f108` | Тест RED → GREEN | Флаги |
|---|---|---|---|---|
| `kt1-telegram` | `f611764`, `d31c796` | 6 файлов, +609 −48 | 9 failed + ошибка сборки `test_tg_text.py` → 17 passed; доп. 2: 3 failed → 20 passed | `TG_API_BASE` (пусто — `api.telegram.org`) |
| `kt1-tests` | `dc845e9` | 1 файл, +15 | полный pytest с системным прокси: 86 failed → 0 failed | — |
| `kt1-p0` | `478491f` | 2 файла, +161 | 3 failed → 3 passed | — |
| `kt1-typeerror` | `5f36433` | 2 файла, +166 −2 | 3 failed, 2 passed → 5 passed | — |
| `kt1-loop` | `7e52bb0` | 8 файлов, +807 −144 | 5 failed, 2 passed → 7 passed | — (пул метрик — константа `METRICS_IO_WORKERS = 2`) |
| `kt1-budget` | `de086d6`, `5c385cd` | 3 файла, +463 −1 | 7 failed, 2 passed → 9 passed; доп. 2: 3 failed → 16 passed | `SEARCH_BUDGET_PER_HOUR`, `NEW_USERS_PER_DAY`, по умолчанию 0 — выключено |

Полный pytest (машина под нагрузкой соседних процессов, очередь КТ0 на паузе, `pgrep -f loadtest.run` пуст):

| Код | Без прокси | С системным прокси |
|---|---|---|
| `367f108` | 984 passed, 3 skipped | **86 failed**, 898 passed, 3 skipped (на `da0b25b` было 87) |
| `kt1-tests` `dc845e9` | 984 passed, 3 skipped | 984 passed, 3 skipped |
| `kt1-p0` `478491f` | 987 passed, 3 skipped | — |
| `kt1-typeerror` `5f36433` | 989 passed, 3 skipped | — |
| `kt1-telegram` `d31c796` | 1004 passed, 3 skipped | — |
| `kt1-budget` `5c385cd` | 1000 passed, 3 skipped | — |
| `kt1-loop` `7e52bb0` | 991 passed, 3 skipped | — |
| `kt1-all` `bdafb14` | **1035 passed**, 3 skipped | **1035 passed**, 3 skipped |

1035 = 984 + 3 (p0) + 5 (typeerror) + 20 (telegram) + 16 (budget) + 7 (loop): тесты веток вместе не мешают друг другу.

## Пробная ветка `kt1-all` и порядок влития (доп. 2, п. 5)

Порядок: `kt1-tests` → `kt1-typeerror` → `kt1-p0` → `kt1-telegram` → `kt1-budget` → `kt1-loop`, каждая — `merge --no-ff`. Сначала маленькие и независимые, потом `bot.py` по возрастанию площади правки, `kt1-loop` — последней, она самая широкая.

Конфликт один — `kt1-budget` после `kt1-telegram`, `src/avia/bot.py`, сразу после `API = …`: обе ветки добавили туда свои константы и функции. Разрешение — оба блока подряд, без правки строк: сначала telegram (`TG_MAX_RETRIES`, `TG_MAX_RETRY_AFTER`, `TG_NO_RETRY_METHODS`, `_retry_after`), затем budget (`SEARCH_BUDGET_WINDOW_SECONDS`, `NEW_PEOPLE_FULL_TEXT`, `_local_day_start`). Коммит `56699b7`. Остальные пары `git merge-tree` сливает без конфликтов; `kt1-loop` легла на пять веток чисто (`bdafb14`).

Проверено после разрешения: проверки бюджета (`_search_budget_wait`, `_note_search_budget`) стоят внутри `_run_search_body`, то есть под `finally` из `kt1-p0`, и сброс `SEARCHING` их покрывает. Отказ по лимиту новых людей на кнопке идёт через `answerCallbackQuery` — с `kt1-telegram` он без повтора на 429 (`TG_NO_RETRY_METHODS`).

## 1. `kt1-telegram` — повтор после 429

**Сделано.** Из `scale-audit-telegram` (`4b351e3`) взяты только `src` и `tests`: `tg_text.py` и `test_tg_text.py` побайтно как там.
- `Bot.call` на 429 ждёт `retry_after` и повторяет не больше 3 раз, потом ERROR в лог и `RuntimeError`, как раньше. Прочие ошибки не повторяются.
- Нарезка длинного HTML по границам тегов, при «can't parse entities» — повтор текстом. Обрезка уведомления подписки — по тегам.
- `TG_API_BASE` — адрес Bot API для моков; пустой — как раньше. `send_photo` тоже идёт по нему, иначе мок слал бы фото с токеном на настоящий `api.telegram.org`.

**Без повтора** (`retries=0`; отступления от `4b351e3`, приняты приёмщиком в доп. 2, п. 6):
- правки «Ищу билеты…» в `WaitMessageUpdater` — пауза держала бы `on_site_done` и весь поиск человека до 3 × 60 с, дольше `LIVE_SAFETY_SECONDS`;
- `deleteMessage` и `editMessageText` «Готово» в `finally` обоих поисков (`bot.py:703, 707, 959, 963` на `d31c796`) — иначе вместе с `kt1-p0` сброс `SEARCHING` ждал бы до 3 × 60 с;
- `answerCallbackQuery` — для всех 15 вызовов через `TG_NO_RETRY_METHODS` (`bot.py:65`): `call()` по умолчанию даёт ему 0 повторов.

**Сверх задания (флаг для приёмщика):** `handle_callback` (`bot.py:1718–1726`) при ошибке `answerCallbackQuery` пишет предупреждение и всё равно выполняет действие кнопки. Без этого 429 на ответ кнопке «Искать» вылетал до `_process_actions`, а `dialog.on_callback` уже поставил `SEARCHING` — сессия застревала без поиска (тест `test_answer_callback_429_does_not_pause_and_search_runs` падал и с `retries=0`). Побочный эффект: любой отказ ответа кнопке (например, просроченное нажатие) теперь тоже не отменяет действие.

**RED** (`367f108`, файлы по одному: без `avia.tg_text` общий запуск обрывается на сборке):
```
FAILED tests/test_bot_telegram_limits.py::test_send_message_delivered_after_retry_after_pause
FAILED tests/test_bot_telegram_limits.py::test_edit_message_text_delivered_after_retry_after_pause
FAILED tests/test_bot_telegram_limits.py::test_gives_up_after_three_retries_and_logs_error
FAILED tests/test_bot_telegram_limits.py::test_retry_after_is_capped
FAILED tests/test_bot_telegram_limits.py::test_retry_pause_does_not_hold_other_peoples_search
FAILED tests/test_bot_telegram_limits.py::test_api_base_override_and_default
FAILED tests/test_bot_telegram_limits.py::test_send_splits_long_html_into_balanced_chunks
FAILED tests/test_bot_telegram_limits.py::test_send_falls_back_to_plain_text_when_html_rejected
FAILED tests/test_bot_telegram_limits.py::test_long_notification_is_cut_on_tag_boundary
9 failed, 2 passed in 0.47s
ERROR tests/test_tg_text.py   ModuleNotFoundError: No module named 'avia.tg_text'
```
Проходят на старом коде по замыслу: `test_other_errors_are_not_retried` (400 не повторяется) и `test_wait_message_progress_edit_does_not_wait_out_429`.

**GREEN** `f611764`: `17 passed in 0.48s`.

**Доп. 2, RED** (`bot.py` с `f611764`, тесты с `d31c796`) → **GREEN** `d31c796`:
```
FAILED test_wait_message_cleanup_429_does_not_pause_search_end[live]   (assert [30.0, 30.0] == [])
FAILED test_wait_message_cleanup_429_does_not_pause_search_end[deep]
FAILED test_answer_callback_429_does_not_pause_and_search_runs
3 failed, 17 passed in 0.58s
---
20 passed in 0.52s
```
Три требования задания: доставка после паузы — `sendMessage` через `Bot.send` и `editMessageText` через `_process_actions(Reply(edit=True))`; повторов не больше 3, потом ERROR в лог; пауза одного человека не держит поиск другого — два настоящих `_run_search`, карточка B доставлена, пока A ждёт.

**Флаги и что не проверено.**
- **Потолок `retry_after` — 60 с** (`TG_MAX_RETRY_AFTER`). Если Telegram попросит больше, бот повторит раньше срока и, скорее всего, снова получит 429; после 3 таких попыток сообщение не доставлено.
- Уведомления подписок теперь пережидают 429 (до 3 × 60 с) в последовательном цикле планировщика: задерживается фон, не живой поиск.
- `TG_API_BASE` не добавлен в пример `.env` — в задании только `src` и `tests`.
- Нет живого прогона с Telegram и `gate --profile kt1`.

## 2. `kt1-tests` — тесты при системном прокси

**Сделано.** Фикстура `_no_system_proxy` (autouse) в `tests/conftest.py` рядом с `_cwd_in_tmp` владельца: убирает `*_proxy` в любом регистре и ставит `NO_PROXY=*` на время теста. Код бота не менялся.

**RED → GREEN** — полный pytest с системным прокси машины (10 переменных `*_proxy` в окружении): на `367f108` — `86 failed, 898 passed, 3 skipped` (число «до»; на `da0b25b` было 87); на `dc845e9` — `984 passed, 3 skipped`. Без прокси — 984 passed на обоих.

**Не проверено.** Прокси только с этой машины; другие схемы (`socks5h`, `NO_PROXY` со списком) не пробовались.

## 3. `kt1-p0` — «⏳ Поиск ещё идёт» навсегда

**Сделано.** `Bot._run_search` стал обёрткой: тело — в `_run_search_body`, в `finally` — `_leave_searching` (сессия в `SEARCHING` → `DONE`). Покрывает отказ «занят», дневной лимит, исключение в поиске и успех.

**RED** (`367f108`) → **GREEN** `478491f`, `tests/test_searching_reset.py`:
```
FAILED tests/test_searching_reset.py::test_exception_in_search_resets_searching
FAILED tests/test_searching_reset.py::test_busy_refusal_resets_searching
FAILED tests/test_searching_reset.py::test_daily_limit_refusal_resets_searching
3 failed in 0.37s
---
3 passed in 0.38s
```
После каждого случая `session.step != SEARCHING`, следующий поиск того же человека запускается.

**Не проверено / замечания.**
- Двойное нажатие кнопки багажа: если отказ «занят» пришёл от ещё идущего поиска того же человека, шаг сразу становится `DONE`. До конца того поиска текст получает меню вместо «⏳ Поиск ещё идёт», результат всё равно приходит. Это следует из требования «после отказа „занят“ `step != SEARCHING`».
- На `367f108` симптом был только у свободного текста: `/start`, `/cancel`, «Найти билет» и кнопка багажа работали. Поэтому «следующий поиск запускается» проходит и на старом коде; RED дают проверки шага и ответа «Поиск ещё идёт».
- `_run_deep_search` в задание не входил: отказ бюджета там ставит `DONE` сам (`kt1-budget`).

## 4. `kt1-typeerror` — повторы при `TypeError` в `pipeline.py`

**Сделано.** `_run_site` (`pipeline.py:1149` и `:1163` на `367f108`) повторяет `_from_browser` без новых аргументов только при несовпадении сигнатуры — `_is_signature_mismatch`, как владелец сделал в `bot._search_call`. `TypeError` внутри парсера больше не даёт второй и третий сбор страницы.

**RED** (`367f108` + тестовый файл) → **GREEN** `5f36433`, `tests/test_pipeline_retry.py`:
```
FAILED tests/test_pipeline_retry.py::test_parser_type_error_collects_page_once_in_browser
FAILED tests/test_pipeline_retry.py::test_parser_type_error_collects_page_once_on_phone
FAILED tests/test_pipeline_retry.py::test_type_error_after_signature_fallback_is_not_retried
3 failed, 2 passed in 0.30s
---
5 passed in 0.25s
```
Проходят на старом коде по замыслу два охранных теста: настоящее несовпадение сигнатуры по-прежнему повторяется.

**Не проверено / замечания.**
- Блок хеджа в `_fetch_browser` (около `pipeline.py:1115`) ловит любое исключение и откатывается на обычный запуск («Сбой в логике хеджей… откат»). `TypeError` парсера в основной копии при активном хедже даёт ещё один обычный запуск — 2 сбора страницы (до правки — до 4). Это не повтор `_run_site`, в задание не входило.
- Как и у владельца в `_search_call`: `TypeError` парсера, в тексте которого есть «unexpected keyword argument», по-прежнему повторится.

## 5. `kt1-loop` — тяжёлое из цикла событий

**Сделано** (строки на `7e52bb0`):
- **Предохранитель:** `check_site_fuse` (`dispatcher.py:112`) можно звать из нескольких потоков: исходы читаются вне замка `_FUSE_LOCK`, решение — под замком и только если состояние сайта не сменилось за чтение; логика и пороги те же, проба по-прежнему одна. Живой поиск зовёт `check_site_fuse_async` (`:157`) — `pipeline.py:964` (старт сайта, бывш. `:953`) и `:1391–1392`.
- **Пул запросов к метрикам** `metrics.run_metrics_io` (`metrics.py:47, 53`, 2 потока): раскладка `plan_live_assignment` (`pipeline.py:1359`), статистика хеджа (`:1009, :1011`), 7 записей попыток через `_record_attempt` (`:92`).
- **Схема `metrics.sqlite`** проверяется раз на файл в процессе (путь + inode; файл заменили или он пуст — заново), `metrics.py:25, 172, 194`.
- **Таблица скоростей** перестраивается одним потоком за раз (`speed.py:81, 439`).
- **Фон:** выбор кандидатов и отметка слота — `asyncio.to_thread` (`bg_collector.py:142, 157`).
- **`/stats`:** текст строит `_stats_text` (`bot.py:977`) в `asyncio.to_thread` (`:1079`).
- **Ограничение выборки:** `popular_routes` (`history.py:911`) и `price_curve` (`:956`) сворачивают поиск в одну строку в SQLite (`GROUP BY`) вместо выгрузки всех цен в Python; ответы те же (тест сверяет со старым расчётом). LIMIT не ставился — он менял бы ответы; окна 7, 30 и 180 дней уже были. `price_curve` владелец уже вынес из цикла на `367f108`, поменян только запрос.

**Тест** `tests/test_loop_responsive.py`: `price_history`, `site_runs` и `attempts` по 100 тыс. строк; таймер спит 10 мс и пишет худшее опоздание, порог 100 мс; провал повторяется до 3 раз (код, который держит цикл, стоит во всех трёх; шум машины — нет). 20 живых поисков × 5 сайтов, 20 `run_once` фона, 20 `/stats`, 20 запросов истории; плюс проверка схемы раз на файл, одна проба на деградировавший сайт при 20 одновременных проверках, сверка свёртки SQL.

**RED** (`367f108` + тестовый файл; очередь КТ0 на паузе, load 2,7):
```
Failed: живой поиск: цикл событий стоял 1217, 1400, 1242 мс за 3 прогона (порог 100 мс)
Failed: кандидаты фона: цикл событий стоял 1458, 1434, 1418 мс за 3 прогона (порог 100 мс)
Failed: /stats: цикл событий стоял 282, 270, 281 мс за 3 прогона (порог 100 мс)
AttributeError: type object 'MetricsStore' has no attribute '_apply_schema'
ImportError: cannot import name 'check_site_fuse_async' from 'avia.dispatcher'
5 failed, 2 passed in 72.50s
```
Проходят на старом коде: история цены (55 мс — уже была вне цикла) и сверка свёртки SQL.

**GREEN** `7e52bb0` (load 6,8 от соседних процессов): `7 passed in 6.69s`; опоздание таймера — живой поиск 20 мс, фон 12 мс, `/stats` 23 мс, история 15 мс.

**Сверх списка задания (флаги для приёмщика), все — по замерам разработчика:**
1. Свой пул из 2 потоков вместо общего `asyncio.to_thread` на пути живого поиска. В общем пуле за сотней коротких запросов к метрикам стояли разборы страниц и не укладывались в `PARSER_TIMEOUT_SECONDS`: 33 таймаута разбора из 133 попыток против 0 со своим пулом. С пулом из 4 потоков тест живого поиска падал 2 раза из 3 при load ≈ 10,7 — поэтому 2.
2. Из цикла ушли ещё статистика хеджа, раскладка и запись попыток: стек-сэмплер показал 16 мс статистики хеджа на старте каждого сайта, а запись в цикле ждала замка SQLite у читателей, которые теперь в потоках.
3. Тронут `speed.py` владельца: без замка 20 поисков перестраивали одну таблицу параллельно.
4. Настройки теста — `price_timeline=False`: CPU `price_timeline` — решение владельца, в КТ1 не трогается.

**Не сделано / не проверено.**
- В цикле остались `record_results`/`record_search` в `bot.py` (не тронуты, чтобы не конфликтовать с `kt1-p0` в `_run_search`), запись `droidhub._record_failure`, перестройка таблицы скоростей при вызове `candidates()`/`get_pair_status` на устаревшей таблице после `invalidate()`. WAL для `metrics.sqlite` убрал бы ожидание читателей писателем — решение владельца.
- Статистика хеджа по-прежнему перебирает 7 дней строк в Python — уже не в цикле.
- **Для симулятора КТ0:** пул метрик идёт через `loop.run_in_executor`, а не `asyncio.to_thread`. Виртуальные часы `loadtest` считают занятыми только потоки `to_thread` (`vclock._threaded_to_thread`, DECISIONS #50) — пока работает поток пула, часы могут прыгнуть. Перед `gate --profile kt1` симулятору нужно учитывать и этот пул (`loadtest/` в ветках КТ1 не менялся — по заданию).
- Очередь из 2 потоков общая на процесс: при 100 поисках разом проверка предохранителя на старте сайта ждёт за записями попыток. Ускорение или замедление старта под нагрузкой покажет только gate.

## 6. `kt1-budget` — бюджет поисков и лимит новых людей

**Сделано.** Два флага, по умолчанию 0 — выключены, поведение как на `367f108`.
- `SEARCH_BUDGET_PER_HOUR` — поисков на весь бот за скользящий час. Сверх него — вежливый отказ «попробуйте через N мин, ваш дневной лимит не списан», до списания лимита человека; `SEARCHING` → `DONE`. Админы не ограничены и не считаются. Счётчик — в памяти (после перезапуска бота — с нуля). И `_run_search`, и `_run_deep_search`.
- `NEW_USERS_PER_DAY` — при `TG_OPEN=1` новых людей за местные сутки. Сверх лимита — вежливый отказ, человек не регистрируется; известных и админов пускает, группы, как раньше, молча отклоняются.
- Доп. 2, п. 7 (`5c385cd`): проверка новых людей не ходит в SQLite в цикле. Известные id — в множестве в памяти, повторно не ищутся. Для неизвестного id `users.get` и `count_since_created` идут через `asyncio.to_thread`; число новых за сутки читается раз в сутки, дальше ведётся в памяти. Место занимается в момент проверки, до `_admit`, без `await` между ними: одновременные новые люди не превышают лимит, два сообщения одного человека занимают одно место. `_new_person_over_limit` стал `async`.

**RED** (`367f108`) → **GREEN** `de086d6`, `tests/test_bot_budget.py`:
```
FAILED test_budget_exhausted_polite_refusal_and_personal_limit_not_charged  (assert 3 == 2: третий поиск стартовал)
FAILED test_admin_not_limited_and_not_counted_by_budget                    (assert 5 == 4)
FAILED test_deep_search_respects_budget                                    (assert 2 == 1)
FAILED test_budget_window_is_rolling_hour       (AttributeError: нет '_search_budget_wait')
FAILED test_new_people_limit_refuses_next_new_person_known_passes  (третий новый получил стартовое меню)
FAILED test_new_people_limit_on_callback        (человек 13 зарегистрирован)
FAILED test_settings_defaults_off_and_env       (AttributeError: нет 'search_budget_per_hour')
7 failed, 2 passed in 0.53s
---
9 passed in 0.45s
```
**Доп. 2, RED** (`de086d6` + тесты `5c385cd`) → **GREEN** `5c385cd`:
```
FAILED tests/test_bot_budget.py::test_known_person_looked_up_at_most_once           (assert 5 <= 1)
FAILED tests/test_bot_budget.py::test_unknown_person_lookups_run_off_event_loop     (запросы в потоке цикла)
FAILED tests/test_bot_budget.py::test_new_people_count_cached_per_day               (assert 5 == 1)
3 failed, 13 passed in 0.55s
---
16 passed in 0.55s
```

**Не проверено / замечания.**
- Счёт новых за сутки в памяти может разойтись с базой: если `_admit` не зарегистрировал человека после проверки, место всё равно занято (ошибка в сторону отказа); админ, впервые написавший сегодня, в памяти не считается, а при перечитывании из базы — считается.
- `_admit` владельца по-прежнему синхронно пишет в `users.sqlite` на каждое сообщение — это не новый код, не трогался.
- Тексты отказов — мои, владелец может переписать.

## Что не проверено во всех ветках

- `gate --profile kt1` — после приёмки КТ0 и переноса gate на актуальный `main` (П20). Для `kt1-loop` симулятору сначала нужно учитывать пул метрик (п. 5).
- Живой прогон с Telegram и неделя в проде — у владельца.
- Полный pytest шёл при работающих соседних процессах (другой проект на этой машине); очередь КТ0 стояла.
- Ветки не пушились; `kt1-all` — пробная, в `main` не вливать.
