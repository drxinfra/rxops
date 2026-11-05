# Транзакции, блокировки, дедлоки

## Два разных вида блокировок

| Что видит пользователь или лог | Чья блокировка | Где смотреть |
|---|---|---|
| «Не удалось установить блокировку», «уже редактирует справочник», `Async ... is locked` | RX: карточку или версию документа держит другой пользователь или процесс | справка RX: admin_snyatie_blokirovki_s_kartochki_objekta.htm (снятие администратором), admin_log_locks_linux.htm (журнал блокировок); данные лежат в таблице `Sungero_System_Locks` (fsd_primery_sistemnykh_tablic.htm) |
| «Запись с идентификатором ... была изменена в другой транзакции» | RX: два процесса сохранили одну запись почти одновременно | журналы сервисов RX: кто участвовал |
| `still waiting for ShareLock`, `canceling statement due to lock timeout`, `deadlock detected`, «сохранение висит» | PostgreSQL: строки или таблицы заняты транзакцией | `pg_locks`, `pg_stat_activity`, pg_check `locks` |

Блокировки RX в `pg_locks` не видны. Карточки, сохранённые и не тронутые дольше `UNCHANGED_CARD_AUTO_UNLOCK_TIMEOUT` (по справке 25.2 по умолчанию 30 минут, admin_web_server_linux.htm), RX разблокирует сам. Строки `Sungero_System_Locks` руками не удаляют: блокировку снимает администратор RX штатно.

Сотни ошибок «Не удалось установить блокировку» в час от фоновых сервисов значат, что обработчики и блоки схем натыкаются на записи пользователей или друг на друга. Это вопрос к прикладной разработке: список записей, время и сервисы. Всплеск после выкладки новой версии часто указывает на новый обработчик, который берёт записи слишком широко.

## Долгие транзакции

Живые долгие транзакции (pg_check `long_xact` показывает то же):

```sql
SELECT pid, usename, application_name, client_addr, state, wait_event_type, wait_event,
       now() - xact_start AS xact_age, now() - state_change AS state_age,
       left(regexp_replace(query, '\s+', ' ', 'g'), 150) AS query
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND xact_start < now() - interval '5 minutes'
ORDER BY xact_start;
```

- `idle in transaction`: приложение открыло транзакцию и молчит. Последний запрос показывает место в коде. Лечится в коде (короткая транзакция, пакетная обработка вместо поштучной), а до исправления `idle_in_transaction_session_timeout` 20-30 минут. После включения таймаута в логах появится `terminating connection due to idle-in-transaction timeout` (25P03): таймаут делает свою работу, сессии надо передать разработчикам.
- `active` с `wait_event_type = Lock`: ждёт блокировку, идите к цепочке ниже.
- `active` с другим ожиданием и долгим `xact_start`: тяжёлый запрос или массовая операция. Сверьте время с расписанием фоновых процессов RX.
- Ручные сессии администраторов в psql, pgAdmin и DBeaver попадают сюда же: открытую транзакцию в консоли закрывают сразу.

## Цепочки блокировок

```sql
SELECT a.pid, pg_blocking_pids(a.pid) AS blocked_by, a.state, now() - a.xact_start AS xact_age,
       a.application_name, left(a.query, 100) AS query
FROM pg_stat_activity a
WHERE cardinality(pg_blocking_pids(a.pid)) > 0;
```

Корень цепочки тот, кто держит блокировку и сам ничего не ждёт (pg_check называет его). Типовые корни:

- `idle in transaction`: зависшая сессия приложения, см. выше;
- сессия с `AccessExclusiveLock` на таблице: `ALTER TABLE`, `REINDEX`, `VACUUM FULL`, `CREATE INDEX` без `CONCURRENTLY`, выкладка прикладной разработки. Такое переносят в окно;
- долгая транзакция интеграции, которая держит строки часами.

В журнале задним числом: при `log_lock_waits = on` строка `still waiting for ... after N ms` и рядом `DETAIL: Process holding the lock: <pid>. Wait queue: ...`.

## Дедлоки 40P01

Дедлок означает, что две транзакции берут одни строки в разном порядке. PostgreSQL откатывает одну из них, повтор обычно проходит. Это штатная защита, лечится порядком обращения к строкам в прикладном коде.

Настройки журнала (перечитываются без перезапуска):

```
log_lock_waits = on
deadlock_timeout = 1s
log_min_duration_statement = 2000
log_line_prefix = '%m [%p] %u@%d app=%a '
```

`%a` пишет имя приложения: по нему видно, какой сервис участвовал. На нагруженной базе заранее проверьте ротацию журналов.

Пакет фактов на один случай, его отдают вендору или своим разработчикам:

1. Полная запись из журнала PostgreSQL с `DETAIL` и `CONTEXT`.
2. Запросы обеих сторон и их приложения (вторая сторона в строках `still waiting` той же секунды или в `log_min_duration_statement`).
3. Что происходило: операция пользователя, фоновый процесс по расписанию.
4. Частота: `grep -c "deadlock detected" <журнал за день>` за последнюю неделю.
5. Версии RX и PostgreSQL.

Пока ждёте исправления: развести фоновые процессы и часы пик, укоротить долгие транзакции интеграций, проверить индексы на колонках внешних ключей (без них изменение родительской строки проверяет всю дочернюю таблицу, ожидания растут).

Не делать: поднимать `deadlock_timeout` (дедлоков столько же, ждать дольше), перезапускать сервисы RX после каждого дедлока (транзакция уже откачена), отправлять вендору одну строку `deadlock detected`.

## Ошибки, которые идут следом

- `current transaction is aborted, commands ignored until end of transaction block` (25P02): следствие. Ищите первую ошибку той же сессии по pid в префиксе журнала.
- `could not execute query` в логах RX: обёртка NHibernate. Код SQLSTATE во внутреннем исключении ведёт к причине: 57014 таймаут, 40P01 дедлок, 42703 и 42P01 схема не совпадает с кодом (неполное обновление), 23503 нарушение внешнего ключа.
- `57P01 terminating connection due to administrator command`: сессию закрыли перезапуском, переключением или `pg_terminate_backend`. Время перезапуска: `SELECT pg_postmaster_start_time();`.

Подробно: https://drxinfra.ru/blog/postgresql-deadlock-40p01.html, https://drxinfra.ru/errors/postgresql-still-waiting-for-lock.html, https://drxinfra.ru/errors/rx-ne-udalos-ustanovit-blokirovku.html, https://drxinfra.ru/errors/rx-record-changed-in-another-transaction.html.
