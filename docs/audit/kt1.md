# КТ1. Шесть веток от `367f108` и пробная `kt1-all`

**Задание:** `docs/review/kt1.md` (приёмщик, 2026-11-07, день) с дополнениями к корректировке 7: 2 (11:30, пп. 5–8) и 3 (12:30, пп. 5–8).
**База:** `upstream/main` на старте — `367f108398b2877c91b219b1bd6451205ce20049`. Все ветки — от неё, каждая в своём worktree `~/avia-scale/kt1-<тема>`. Только локально: без push, PR и влития в `main` и `kt0-upstream`.
**Пробная ветка:** `kt1-all` (`~/avia-scale/kt1-all`) — все шесть веток вместе, тоже от `367f108`. Нужна, чтобы проверить влитие и полный pytest; в `main` не вливается. Этот отчёт лежит в ней отдельным коммитом.

## Итог

| Ветка | Коммиты | `diff --stat 367f108` | Тест RED → GREEN | Флаги |
|---|---|---|---|---|
| `kt1-telegram` | `f611764`, `d31c796` | 6 файлов, +609 −48 | 9 failed + ошибка сборки `test_tg_text.py` → 17 passed; доп. 2: 3 failed → 20 passed | `TG_API_BASE` (пусто — `api.telegram.org`) |
| `kt1-tests` | `dc845e9` | 1 файл, +15 | полный pytest с системным прокси: 86 failed → 0 failed | — |
| `kt1-p0` | `478491f`, `88f8a4e`, `524ca44` | 2 файла, +281 −4 | 3 failed → 3 passed; доп. 3: 3 failed, 2 passed → 5 passed; доп. 6: 1 failed, 5 passed → 6 passed | — |
| `kt1-typeerror` | `5f36433` | 2 файла, +166 −2 | 3 failed, 2 passed → 5 passed | — |
| `kt1-loop` | `7e52bb0`, `f6b27e9` | 12 файлов, +1065 −172 | 5 failed, 2 passed → 7 passed; доп. 3: 2 failed, 1 passed → 3 passed | — (пулы метрик — константы: чтения 2 потока, записи 1) |
| `kt1-budget` | `de086d6`, `5c385cd` | 3 файла, +463 −1 | 7 failed, 2 passed → 9 passed; доп. 2: 3 failed → 16 passed | `SEARCH_BUDGET_PER_HOUR`, `NEW_USERS_PER_DAY`, по умолчанию 0 — выключено |
| `kt1-refund` (П29, от `9ba5ae3`) | `0bc40e0`, `6ec60e9`, `7342517` | `diff --stat 9ba5ae3`: 3 файла, +546 −9 (src: 2 файла, +70 −9) | 15 failed, 8 passed → 23 passed; по ревью → 28 passed | — (багфикс) |

Полный pytest — только в паузах очереди КТ0 (`pgrep -f loadtest.run` пуст; 12:00–12:06 и 13:56–13:58), рядом бывали процессы другого проекта на этой машине:

| Код | Без прокси | С системным прокси |
|---|---|---|
| `367f108` | 984 passed, 3 skipped | **86 failed**, 898 passed, 3 skipped (на `da0b25b` было 87) |
| `kt1-tests` `dc845e9` | 984 passed, 3 skipped | 984 passed, 3 skipped |
| `kt1-p0` `478491f` | 987 passed, 3 skipped | — |
| `kt1-p0` `88f8a4e` (доп. 3) | 989 passed, 3 skipped | — |
| `kt1-typeerror` `5f36433` | 989 passed, 3 skipped | — |
| `kt1-telegram` `d31c796` | 1004 passed, 3 skipped | — |
| `kt1-budget` `5c385cd` | 1000 passed, 3 skipped | — |
| `kt1-loop` `7e52bb0` | 991 passed, 3 skipped | — |
| `kt1-loop` `f6b27e9` (доп. 3) | 994 passed, 3 skipped | — |
| `kt1-all` `bdafb14` | 1035 passed, 3 skipped | 1035 passed, 3 skipped |
| `kt1-all` `aba9321` (доп. 3) | **1040 passed**, 3 skipped | **1040 passed**, 3 skipped |
| `kt1-p0` `524ca44` (доп. 6) | 990 passed, 3 skipped | — |
| `kt1-all` `699f40c` (доп. 6) | **1042 passed**, 3 skipped | — |
| `kt1-refund` `6ec60e9` (П29) | 1015 passed, 3 skipped | — |
| `kt1-refund` `7342517` (П29) | 1016 passed, 3 skipped | — |
| `kt1-all` `4bc6e4d` (П29, с `main` `9ba5ae3`) | **1080 passed**, 3 skipped | — |

1040 = 984 + 5 (p0) + 5 (typeerror) + 20 (telegram) + 16 (budget) + 10 (loop): тесты веток вместе не мешают друг другу. Доп. 6: 990 = 989 + 1 (тест SQLite), 1042 = 1040 + 2 (тест SQLite и тест бюджета).

Доп. 6: полный pytest — 2026-11-08, 04:14–04:17, после коммита КТ0 `db84cc5` в `kt0-up` и двух пустых `pgrep -f 'loadtest.run|pytest'` подряд (в виде `[l]oadtest.run|[p]ytest`: голый шаблон ловит и сам цикл ожидания, и сторож сессии КТ0, в тексте которых есть эти слова); по одному, без прокси. Интерпретатор — `~/avia-scale/upstream/.venv` только на чтение (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`): в нём есть `matplotlib`, нет `sklearn` — 3 skipped, как в строках выше. В `~/avia-scale/work/.venv` нет `matplotlib`, там `test_charts.py` пропускается целиком: `524ca44` — 986 passed, 4 skipped.

`tests/test_loop_responsive.py` в паузе очереди: `kt1-loop` `f6b27e9` — 7 passed (опоздание таймера: живой поиск 24 мс, фон 14 мс, `/stats` 4 мс, история 18 мс); `kt1-all` `aba9321` — 7 passed (24, 13, 7, 32 мс); доп. 6, `kt1-all` `699f40c` (04:17, сразу после полного прогона, load 3,9) — 7 passed in 7.12s (32, 12, 6, 38 мс). Порог — 100 мс.

Доп. 4 (16:20): `kt1-p0` `88f8a4e`, `kt1-loop` `f6b27e9` и этот файл (`47bd70e`) приёмщик принял по коду; полный pytest и `tests/test_loop_responsive.py` он повторит сам, когда очередь КТ0 встанет. П. 5 (пул записей и часы симулятора) — в разделе 5.

Доп. 5 (21:20): прямой тест порядка записи и чтения принят вместо «один seed дважды» — делается при переносе gate (раздел 5). Полный pytest `kt1-p0`, `kt1-loop`, `kt1-all` и `tests/test_loop_responsive.py` приёмщик повторит сам после очереди КТ0.

Доп. 6 (2026-11-08, 03:00): владелец решил закрыть последнюю дыру `kt1-p0` — исключение SQLite в проверках до `busy.add` оставляло `SEARCHING`. `kt1-p0` `524ca44`, влито в `kt1-all` (`699f40c`, конфликт 3); подробности — раздел 3, «Доп. 6».

Доп. 7 (2026-11-08, день): П29 — возврат суточной попытки, ветка `kt1-refund` от `main` `9ba5ae3`, раздел 7. Её влитие внесло в `kt1-all` четыре коммита владельца `367f108..9ba5ae3` (лимит 3 отслеживания, кэши, повтор пустого задания на другом телефоне): `gate --profile kt1` будут мерить против базы `9ba5ae3`. Полный pytest — без прокси, тем же интерпретатором, в паузах прогонов КТ0 (`pgrep -f '[l]oadtest.run|[-]m pytest'` пуст перед каждым запуском): `kt1-refund` `6ec60e9` — 12:52:01–12:52:20; `kt1-refund` `7342517` — 13:00:09–13:00:29 (19 с); `kt1-all` `4bc6e4d` — 13:00:29–13:01:01 (31 с); `tests/test_loop_responsive.py` на `kt1-all` `4bc6e4d` — 13:01:01–13:01:08, 7 passed in 7.22s (load 5,1 в конце). Перед каждым из трёх запусков `pgrep` был пуст; шли подряд, без `nice`: окна ждал не дольше 6 мин (12:44–12:46, 12:48–12:51, 12:53–12:59 — прогоны КТ0 `loadtest.run` и pytest в `kt0-up`). 1015 = 988 (`9ba5ae3`: 984 + 4 теста `test_droidhub_jobs_retry.py`) + 27 (`test_search_refund.py`); 1016 = 988 + 28; 1080 = 1042 (`699f40c`) + 4 (`test_droidhub_jobs_retry.py`) + 28 (`test_search_refund.py`) + 6 (`test_search_refund_budget.py`).

## Пробная ветка `kt1-all` и порядок влития (доп. 2, п. 5)

Порядок: `kt1-tests` → `kt1-typeerror` → `kt1-p0` → `kt1-telegram` → `kt1-budget` → `kt1-loop`, каждая — `merge --no-ff`. Сначала маленькие и независимые, потом `bot.py` по возрастанию площади правки, `kt1-loop` — последней, она самая широкая.

Правки доп. 3 влиты в `kt1-all` повторно: `kt1-p0` (`88f8a4e`) → `8b70265`, `kt1-loop` (`f6b27e9`) → `aba9321`. Доп. 6: `kt1-p0` (`524ca44`) → `699f40c`.

**Конфликт 1** — `kt1-budget` после `kt1-telegram`, `src/avia/bot.py`, сразу после `API = …`: обе ветки добавили туда свои константы и функции. Разрешение — оба блока подряд, без правки строк: сначала telegram (`TG_MAX_RETRIES`, `TG_MAX_RETRY_AFTER`, `TG_NO_RETRY_METHODS`, `_retry_after`), затем budget (`SEARCH_BUDGET_WINDOW_SECONDS`, `NEW_PEOPLE_FULL_TEXT`, `_local_day_start`). Коммит `56699b7`. Остальные пары `git merge-tree` сливает без конфликтов; `kt1-loop` легла на пять веток чисто (`bdafb14`).

**Конфликт 2** — повторное влитие `kt1-p0` (`88f8a4e`), `src/avia/bot.py`: `kt1-p0` убрал обёртку `_run_search` / `_run_search_body`, а в `kt1-all` прямо перед ней стояли функции бюджета. Оставлены функции бюджета, старая обёртка убрана. Проверки бюджета стоят в `_run_search` до `busy.add`: отказ бюджета сам ставит `DONE` (`_refuse_over_budget`), как отказ по лимиту; `SEARCHING` снимается в `finally` сразу после `busy.discard`, повторы на 429 там выключены (`kt1-telegram`). Коммит `8b70265`; `kt1-loop` (`f6b27e9`) легла чисто (`aba9321`).

**Конфликт 3** — доп. 6, влитие `kt1-p0` (`524ca44`), `src/avia/bot.py`, `_run_search`: `kt1-p0` взял проверку дневного лимита под `try`, а `kt1-budget` вставил перед ней `_search_budget_wait` / `_refuse_over_budget`, после — `_note_search_budget`. Разрешение (`bot.py:571–597` на `699f40c`): один `try` накрывает весь код до `busy.add` без `await` — `_search_budget_wait`, `try_use_search` (только если бюджет не исчерпан: сверх бюджета лимит человека не списывается, как в `kt1-budget`) и `_note_search_budget` (только если поиск пускаем). Отказы по бюджету и по лимиту — после `try`, тексты и порядок прежние; оба снимают `SEARCHING` до своего `await`. Тест на бюджет — только в `kt1-all` (раздел 3, «Доп. 6»). При влитии в `main` владелец встретит тот же конфликт: разрешение — как здесь.

**Конфликт 4** — доп. 7, влитие `kt1-refund` (`0bc40e0`, от `9ba5ae3`) → `002e3bf`, `src/avia/bot.py`, пять мест:
- помощники перед `_run_search`: оставлены оба набора — `_search_budget_wait`, `_note_search_budget`, `_refuse_over_budget`, `_leave_searching` (`kt1-budget`, `kt1-p0`) и `_try_charge_search`, `_refund_search` (`kt1-refund`);
- `_run_search`, проверки до `busy.add`: списание осталось под общим `try` из `kt1-p0`, после проверки бюджета. `charged_at` ставится там же, только если лимит пустил (`_try_charge_search`); сверх бюджета `_try_charge_search` не вызывается — ни списания, ни возврата. Если тот же `try` упал уже после списания (например, в `_note_search_budget`), попытка возвращается до ответа «⚠️ Не получилось начать поиск»; SQLite упал в самом списании — `charged_at` пуст, возвращать нечего;
- `finally` живого и глубокого поиска: сначала возврат (синхронный, без `await`), потом `busy.discard` и `_leave_searching` подряд, как в `kt1-p0`;
- `_run_deep_search`: шаг 3 — бюджет (`kt1-budget`), шаг 4 — списание через `_try_charge_search`. Молчание глубокого поиска при ошибке базы до `busy.add` не тронуто (вопрос открыт).

Остальное от `9ba5ae3` легло без конфликтов (`pipeline.py` — `key_query`/`cache_if` владельца рядом с `_record_attempt_nowait` из `kt1-loop`). Тесты по ревью (`6ec60e9`) → `094207a` без конфликтов; `7342517` → `dad0d32`. Тест на бюджет и общий `try` — только в `kt1-all`, `tests/test_search_refund_budget.py` (`4bc6e4d`), раздел 7. При влитии в `main` владелец встретит тот же конфликт: разрешение — как здесь.

Проверено после разрешения: тесты трёх веток, правивших `bot.py` (`test_searching_reset.py`, `test_bot_budget.py`, `test_bot_telegram_limits.py`) — 35 passed на `aba9321`. Отказ по лимиту новых людей на кнопке идёт через `answerCallbackQuery` — с `kt1-telegram` он без повтора на 429 (`TG_NO_RETRY_METHODS`).

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

**Сделано** (`88f8a4e`, доп. 3, п. 6 — исправление задания: отказ «занят» означает, что у человека уже идёт свой поиск).
- `_leave_searching(chat_id)` (`bot.py:479–488` на `88f8a4e`): `SEARCHING` → `DONE`, другой шаг не трогает.
- Отказ «занят» (`bot.py:492`) шаг не трогает: `SEARCHING` снимет `finally` идущего поиска.
- Отказ по дневному лимиту (до `busy.add`, `:499`) снимает шаг сразу; в `kt1-all` так же — отказ бюджета.
- `finally` живого (`:687–689`) и глубокого (`:942–944`) поиска: `_leave_searching` сразу после `busy.discard`, без `await` между ними — иначе новый поиск успел бы взять `busy`, а этот `finally` снял бы его `SEARCHING`. Обёртка `_run_search` / `_run_search_body` из `478491f` убрана: у неё был именно этот зазор.

**RED** (`367f108`) → **GREEN** `478491f`, `tests/test_searching_reset.py`:
```
FAILED tests/test_searching_reset.py::test_exception_in_search_resets_searching
FAILED tests/test_searching_reset.py::test_busy_refusal_resets_searching
FAILED tests/test_searching_reset.py::test_daily_limit_refusal_resets_searching
3 failed in 0.37s
---
3 passed in 0.38s
```
**Доп. 3, RED** (`478491f`) → **GREEN** `88f8a4e`: `test_busy_refusal_resets_searching` заменён на два —
второе нажатие во время поиска (текст получает «⏳ Поиск ещё идёт», после конца первого поиска `step == DONE`) и нажатие «Искать» во время глубокого поиска (после его конца `step != SEARCHING`, в двух вариантах — успех и ошибка глубокого поиска).
```
FAILED tests/test_searching_reset.py::test_second_press_while_search_running_keeps_searching    (DONE == SEARCHING)
FAILED tests/test_searching_reset.py::test_press_while_deep_search_running_cleared_by_deep[deep_ok]
FAILED tests/test_searching_reset.py::test_press_while_deep_search_running_cleared_by_deep[deep_error]
3 failed, 2 passed in 0.42s
---
5 passed in 0.40s
```
Каждая новая строка проверена откатом по одной: без `:499` падает тест лимита, без `:689` — тест исключения, без `:944` — `deep_error`.

**Доп. 6 (`524ca44`): исключение до `busy.add`.** `users.try_use_search` ходит в SQLite до `busy.add`; на `88f8a4e` исключение там (`sqlite3.OperationalError: database is locked`) вылетало из обработчика кнопки (`asyncio` пишет «Task exception was never retrieved» без `chat_id`), шаг оставался `SEARCHING`, и на любой текст человек получал «⏳ Поиск ещё идёт» до `/start` или `/cancel`.
- `_run_search` (`bot.py:495–504` на `524ca44`): проверка лимита под `try`; на исключение — `log.exception("не получилось начать поиск, chat_id=%s", …)`, `_leave_searching` и «⚠️ Не получилось начать поиск, попробуйте ещё раз.» Отказ по лимиту (`:505`) и отказ «занят» не менялись. Внутри `try` — только синхронная проверка, без `await`; тексты отказов уходят уже после `_leave_searching`.
- В `kt1-all` (`699f40c`) под тем же `try` — `_search_budget_wait` и `_note_search_budget` (конфликт 3). `_search_budget_wait` считает в памяти (`self._budget_times`), в SQLite не ходит, но стоит до `busy.add` и тоже защищён.
- Глубокий поиск не тронут: шаг `SEARCHING` он не ставит — действие `deep` в `dialog.py:702–717` возвращает `DeepSearchRequest`, шаг не меняя (после живого поиска он `DONE`), а чужой `SEARCHING` в это время принадлежит живому поиску, который его и снимет. Поэтому теста на глубокий поиск нет.

**Доп. 6, RED** (`88f8a4e`) → **GREEN** `524ca44`, `tests/test_searching_reset.py::test_db_error_in_daily_limit_resets_searching` (`try_use_search` бросает `OperationalError` один раз: шаг не `SEARCHING`, `busy` пуст, текст ошибки, одна запись `avia.bot` с `exc_info` и `chat_id`; следующий поиск того же человека идёт):
```
src/avia/bot.py:497: in _run_search
    and not self.users.try_use_search(chat_id, self.s.daily_search_limit)
E           sqlite3.OperationalError: database is locked
FAILED tests/test_searching_reset.py::test_db_error_in_daily_limit_resets_searching
1 failed, 5 passed in 0.65s
---
6 passed in 0.91s
```
**Доп. 6, `kt1-all`**, `test_budget_error_resets_searching` (`_search_budget_wait` бросает `RuntimeError` один раз: то же плюс `total_searches == 0` — лимит не списан). RED — на копии `df6022e` (до влития) и на копии с «наивным» разрешением (под `try` только лимит из `kt1-p0`, бюджет снаружи); GREEN — `699f40c`:
```
df6022e:  src/avia/bot.py:577: E sqlite3.OperationalError: database is locked
          src/avia/bot.py:571: E RuntimeError: бюджет сломан
          2 failed, 5 passed in 0.55s
наивное:  src/avia/bot.py:571: E RuntimeError: бюджет сломан
          FAILED tests/test_searching_reset.py::test_budget_error_resets_searching
          1 failed, 6 passed in 0.52s
---
699f40c:  tests/test_searching_reset.py tests/test_bot_budget.py — 23 passed in 0.84s
```

**Не проверено / замечания.**
- Гонка нового нажатия: диалог ставит `SEARCHING` до проверки `busy`, между ними `await answerCallbackQuery`. Если первый поиск кончится в этом окне, его `finally` снимет `SEARCHING` нового нажатия; новый поиск идёт с шагом `DONE` — человек не застревает, но до конца поиска текст получает меню.
- ~~Ошибка SQLite в `users.try_use_search` (до `busy.add`) оставляет `SEARCHING`, как на `367f108`; обёртка `478491f` этот случай закрывала.~~ Закрыто в доп. 6 (`524ca44`).
- Доп. 6: в `kt1-all` `_note_search_budget` стоит в `try` после `try_use_search`; если бы он бросил, лимит человека уже списан, а поиска нет. Это копия списка в памяти, на практике не бросает; теста нет.
- Доп. 6: проверки глубокого поиска до `busy.add` (`try_use_search`, в `kt1-all` и `_search_budget_wait`, `bot.py:860–877` на `699f40c`) не защищены: исключение там по-прежнему вылетает из обработчика кнопки без ответа человеку. `SEARCHING` при этом не застревает (глубокий поиск его не ставит), поэтому в доп. 6 не трогал.
- Готовый результат глубокого поиска из кэша ставит `DONE` текущей сессии, даже если идёт живой поиск, — код владельца, успех, не трогал.
- На `367f108` симптом был только у свободного текста: `/start`, `/cancel`, «Найти билет» и кнопка багажа работали.
- `_run_deep_search` получил `_leave_searching` в `finally`; его отказы «занят» и по лимиту не менялись (отказ бюджета там ставит `DONE` сам — `kt1-budget`).

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

**Доп. 3, п. 5 (`f6b27e9`): готовая цена не ждёт записи попытки.** На `7e52bb0` `_from_browser` делал `await _record_attempt(...)` перед `return offers`: запись стояла в очереди пула за чтениями, и это была новая точка отмены — при отмене поиска в ожидании записи собранные цены сайта пропадали.
- Роль попытки (`_attempt_role`, `pipeline.py:49` — тот же код, что был в `:68–71`), время попытки и база метрик берутся в цикле событий в месте вызова, запись уходит в поток без ожидания — `_record_attempt_nowait` (`:61`), во всех 7 местах `_from_browser` (`:654, 687, 731, 787, 807, 827, 874`).
- `metrics.submit_metrics_write` держит ссылку на каждую запись в полёте и пишет её ошибку в лог (`log.error`, с исключением); `metrics.wait_metrics_io(timeout)` ждёт все записи — для тестов, которые читают `attempts` после поиска (`test_attempts_fuse.py`, `test_hedge.py`, `test_fetch_jobs.py`, `test_loop_responsive.py`).
- **Отступление (флаг):** записи идут не в пул чтений (2 потока), а в свой пул из одного потока `_METRICS_WRITE_POOL`. С двумя потоками записи могли бы закоммититься не по порядку — `id` в `attempts` перестал бы идти порядком попыток (`test_attempts_fuse.py`, `ORDER BY id`), и записи не стоят в очереди за чтениями. Время попытки теперь — время попытки, а не записи (раньше `record_attempt` брал время записи).

**Доп. 3, RED** (`7e52bb0`) → **GREEN** `f6b27e9`, `tests/test_attempt_write_nowait.py` (пул метрик занят задачами по 2 с, настоящий `time.sleep`):
```
FAILED tests/test_attempt_write_nowait.py::test_offers_return_while_metrics_pool_is_busy    (assert 1.946 < 0.1)
FAILED tests/test_attempt_write_nowait.py::test_failed_attempt_write_is_logged_and_offers_kept   (RuntimeError: диск переполнен — цены потеряны)
2 failed, 1 passed in 4.37s
---
3 passed in 4.32s
```
Проходит на обоих по замыслу: роли и время попыток (капча на dev1, цены на dev2 при занятом пуле → после слива пула строки `(dev1, captcha, primary)`, `(dev2, ok, retry)`).

**Сверх списка задания (флаги для приёмщика), все — по замерам разработчика:**
1. Свой пул из 2 потоков вместо общего `asyncio.to_thread` на пути живого поиска. В общем пуле за сотней коротких запросов к метрикам стояли разборы страниц и не укладывались в `PARSER_TIMEOUT_SECONDS`: 33 таймаута разбора из 133 попыток против 0 со своим пулом. С пулом из 4 потоков тест живого поиска падал 2 раза из 3 при load ≈ 10,7 — поэтому 2.
2. Из цикла ушли ещё статистика хеджа, раскладка и запись попыток: стек-сэмплер показал 16 мс статистики хеджа на старте каждого сайта, а запись в цикле ждала замка SQLite у читателей, которые теперь в потоках.
3. Тронут `speed.py` владельца: без замка 20 поисков перестраивали одну таблицу параллельно.
4. Настройки теста — `price_timeline=False`: CPU `price_timeline` — решение владельца, в КТ1 не трогается.

**Не сделано / не проверено.**
- В цикле остались `record_results`/`record_search` в `bot.py` (не тронуты, чтобы не конфликтовать с `kt1-p0` в `_run_search`), запись `droidhub._record_failure`, перестройка таблицы скоростей при вызове `candidates()`/`get_pair_status` на устаревшей таблице после `invalidate()`. WAL для `metrics.sqlite` убрал бы ожидание читателей писателем — решение владельца.
- Статистика хеджа по-прежнему перебирает 7 дней строк в Python — уже не в цикле.
- **Для симулятора КТ0 (доп. 3, п. 7, подтверждено приёмщиком по коду):** пул чтений идёт через `loop.run_in_executor` (`metrics.py:57`) мимо счётчика потоков виртуальных часов (`vclock.py:72–83` при `threads_busy == 0` прыгают к следующему таймеру). Пока поток пула читает SQLite, виртуальное время убегает до следующего таймера: 1,5 с тишины, 15 с автоотмены, 90 с предохранителя. Сделать при переносе gate на базу КТ1 (П20, обслуживание), не сейчас: считать потоки в `VirtualLoop.run_in_executor`, причём один раз — `asyncio.to_thread` сам идёт через `loop.run_in_executor`. Тест в `loadtest`: функция через `run_metrics_io` спит 0,2 с по-настоящему, таймер на +60 с → виртуальное время сдвинулось меньше чем на 1 с. Пул записей (доп. 4, п. 5, приёмщик): записи идут через `_METRICS_WRITE_POOL.submit`, мимо `loop.run_in_executor`, и счётчик в `VirtualLoop.run_in_executor` их не увидит. Пока поток записи работает, часы могут прыгнуть к следующему таймеру, и статистика хеджа прочтёт `attempts` без последней попытки; в проде запись доходит за миллисекунды. Готово при переносе gate: поток записи тоже держит `threads_busy` — обёртка над `submit_metrics_write` в `loadtest`, `src` не трогать. Проверку «один seed дважды, у каждого человека одинаковые `card_s`» приёмщик заменил прямым тестом (доп. 5, п. 5): на нынешних часах она не сходится и без пула записей: CPU потоков `to_thread` входит в виртуальное время, и в 18 из 40 пар «тот же seed дважды» из добора КТ0 (`0b4de93`, тихая машина) `card_s` разошлась хоть у одного человека — обычно на 0,1–0,8 с, в 3 парах на 9,7–57,4 с (`docs/audit/kt0_upstream.md`, доп. 4, п. 3). Готово при переносе gate: тест в `loadtest` — запись через `submit_metrics_write` спит 0,2 с по-настоящему, таймер на +60 с; чтение после таймера видит запись, а виртуальное время за запись сдвинулось меньше чем на 1 с.
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

## 7. `kt1-refund` — возврат суточной попытки (П29, работа 10)

**Задание:** `docs/audit/07_plan.md`, КТ1, работа 10 (2026-11-08). Ветка `kt1-refund` — от `main` `9ba5ae3` (не от `367f108`, как шесть веток выше), worktree `~/avia-scale/kt1-refund`. Смысл перенесён с нашего `6809085` и невлитого `1733a02` владельца (`pool/f60264b93650`), код не копировался.

**Правило.** `_run_search` и `_run_deep_search` списывают попытку до поиска. Попытка возвращается, если её списали (человек не из `TG_ALLOWED_IDS`, лимит пустил) и человек не получил цен:
- исключение в теле поиска после списания — тот же `except`, что шлёт «⚠️ …»;
- ни у одного `SiteResult` нет `ok`: все сайты упали; сработал предохранитель, и неответившие стали «сайт временно недоступен»; пустой `results`;
- по тому же смыслу (в задании не названо): поиск отменён (`CancelledError`, остановка бота), пока ни один сайт не ответил.

Не возвращается: хоть один сайт `ok`, даже с 0 предложений («рейсов нет»); ответ из кэша глубокого поиска, отказы «занят», по бюджету и по лимиту — там не списывали. Бюджет бота на час (`_note_search_budget`) не возвращается: телефоны работу сделали. Тексты ответов не менялись, новых сообщений нет. Багфикс, без флага.

**Где правка** (`git diff --stat 9ba5ae3 kt1-refund`: 3 файла, +546 −9; `src` — 2 файла, +70 −9):
- `users.py`: `charge_search(tg_id, limit)` — обёртка над `try_use_search` владельца (его API не менялось), возвращает момент списания или `None`. `refund_search(tg_id, charged_at)` — день берётся из момента списания (`_search_day`, общий с `try_use_search`). Счёт дня уменьшается, только если строка ещё на дне списания и счёт больше 0 — поиск на стыке суток не трогает счёт нового дня; `total_searches` уменьшается всегда, не ниже 0; нет строки — ничего.
- `bot.py`: `_try_charge_search` (можно ли искать, момент списания), `_refund_search` (ошибка базы — `log.exception` с `chat_id`, ответ человеку не ломается). В обоих поисках флаг `keep_charge`: `any(r.ok for r in results)` сразу после сборки `results` (после предохранителя тоже), `False` в `except`; в `finally` — один вызов `_refund_search`, если `charged_at` есть и `keep_charge` ложно. Одно списание — не больше одного возврата по построению.
- Время списания берёт `UserStore`, а не `bot.py` (`7342517`): тест `kt1-p0` `test_daily_limit_refusal_resets_searching` переводит часы подменой `avia.users.time`; с `now=time.time()` из `bot.py` он падал на `kt1-all` `094207a` — «завтра» лимит не сбрасывался.

**Коммиты `kt1-refund`:** `0bc40e0` (правка и тесты), `6ec60e9` (тесты по ревью), `7342517` (`charge_search`). Влиты в `kt1-all`: `002e3bf` (конфликт 4), `094207a`, `dad0d32`; тест бюджета — `4bc6e4d`.

**RED** (`9ba5ae3` + `tests/test_search_refund.py`, код не тронут) → **GREEN** `0bc40e0`:
```
FAILED test_refund_returns_attempt_of_the_charge_day               (AttributeError: нет refund_search)
FAILED test_refund_without_user_row_does_nothing                   (то же)
FAILED test_refund_never_goes_below_zero                           (то же)
FAILED test_refund_for_previous_day_keeps_new_day_count            (то же)
FAILED test_live_search_exception_refunds_attempt                  (assert 4 == 3)
FAILED test_live_no_site_answered_refunds_attempt[all_sites_down]  (assert 4 == 3)
FAILED test_live_no_site_answered_refunds_attempt[no_results]      (assert 4 == 3)
FAILED test_live_safety_fired_with_no_answers_refunds_attempt      (assert 4 == 3)
FAILED test_deep_search_failure_refunds_attempt[exception]         (assert 4 == 3)
FAILED test_deep_search_failure_refunds_attempt[all_sites_down]    (assert 4 == 3)
FAILED test_refund_db_error_keeps_normal_reply[live]               (assert [] == [555]: возврата не было)
FAILED test_refund_db_error_keeps_normal_reply[deep]               (то же)
FAILED test_refund_across_midnight_keeps_new_day_count             (('2026-11-09', 1, 5) == ('2026-11-09', 1, 4))
FAILED test_one_charge_one_refund_when_reply_send_fails[live]      (assert 4 == 3)
FAILED test_one_charge_one_refund_when_reply_send_fails[deep]      (assert 4 == 3)
15 failed, 8 passed in 0.88s
---
23 passed in 0.59s
```
8 прошедших в RED — охранные: «сайт ответил — списано» (живой и глубокий), кэш глубокого, админ, отказы «занят» и по лимиту.

**По ревью** (`python-reviewer`, только чтение, на `0bc40e0`: в коде ошибок нет, в тестах не покрыт `keep_charge = False` в `except`) → `6ec60e9`: исключение при отправке выдачи после ответа сайтов — один возврат, живой и глубокий; отмена поиска до ответа сайтов — возврат; полночь — и для глубокого; `time.time` на полдень дня D на весь модуль, чтобы счёт за день не зависел от часа прогона. Мутация — копия `6ec60e9` без двух строк `keep_charge = False`: `2 failed, 25 passed` (`test_exception_after_site_answered_refunds_once[live|deep]`); сам `6ec60e9` — 27 passed. `7342517`: + `test_charge_returns_charge_moment_or_none_over_limit` — 28 passed.

**`kt1-all`, тест бюджета и общего `try`** — `tests/test_search_refund_budget.py` (`4bc6e4d`, 6 тестов): сверх бюджета — ни списания, ни возврата (живой и глубокий); SQLite упал в самом списании под `try` из `kt1-p0` — возвращать нечего; списали, а тот же `try` упал до поиска — один возврат; возврат не отдаёт место в часовом бюджете. RED — на копиях `094207a` с «наивным» разрешением, GREEN — `dad0d32` + тест:
```
копия без возврата в общем except:  FAILED test_charged_but_search_not_started_refunds_once (assert [] == [555])
                                    1 failed, 5 passed in 0.32s
копия со списанием до бюджета:      FAILED test_budget_refusal_neither_charges_nor_refunds[live] (assert 4 == 3)
                                    FAILED test_budget_refusal_neither_charges_nor_refunds[deep] (assert 4 == 3)
                                    2 failed, 4 passed in 0.29s
---
kt1-all (тест бюджета, test_search_refund, test_searching_reset, test_bot_budget, test_bot_telegram_limits): 71 passed in 1.63s
```
На `094207a` (до `7342517`) тот же набор без `test_bot_telegram_limits.py`: `FAILED test_searching_reset.py::test_daily_limit_refusal_resets_searching`, 1 failed, 55 passed.

**Не проверено / замечания.**
- Исключение после того, как часть выдачи ушла (упал второй кусок), тоже возвращает попытку: по правилу «исключение в теле — возврат». Редко и в пользу человека.
- На стыке суток `total_searches` уменьшается, счёт дня — нет (строка уже на новом дне).
- Возврат — синхронный запрос к `users.sqlite` в цикле событий, как и само списание.
- Глубокий поиск в `kt1-all`: бюджет и списание стоят вне `try` (место открытого вопроса «молчание при ошибке базы до `busy.add`», `bot.py` около 890–905). Если между списанием и `busy.add` что-то упадёт, попытка не вернётся — не трогал.
- `droidhub._record_failure`: новая запись владельца `metrics.record_attempt(outcome="empty")` идёт мимо `_record_attempt_nowait` — не правил, по заданию.
- `gate --profile kt1` не запускался: loadtest не трогаю.

## Что не проверено во всех ветках

- `gate --profile kt1` — после приёмки КТ0 и переноса gate на актуальный `main` (П20). Для `kt1-loop` симулятору сначала нужно учитывать оба пула метрик, чтения и записи (доп. 3, п. 7; доп. 4, п. 5).
- Живой прогон с Telegram и неделя в проде — у владельца.
- Полный pytest шёл при работающих соседних процессах (другой проект на этой машине); очередь КТ0 стояла.
- Ветки не пушились; `kt1-all` — пробная, в `main` не вливать.
