# Ошибки PostgreSQL в логах RX: что значат и куда идти

Шаг означает пункт «Порядка работы» в SKILL.md. Разбор каждой ошибки: https://drxinfra.ru/errors/ и имя статьи из последней колонки с расширением `.html`.

## Соединения и доступ

| Текст | Что значит | Шаг | Статья |
|---|---|---|---|
| `FATAL: sorry, too many clients already` (53300), `remaining connection slots are reserved` | занят `max_connections` | 3 | postgresql-too-many-clients |
| `no pg_hba.conf entry for host` | нет правила для адреса, пользователя, базы и типа шифрования | 12 | postgresql-no-pg-hba-conf-entry |
| `pg_hba.conf rejects connection for host` | первым подошло правило `reject`; смотрите концовку `SSL encryption` или `no encryption` | 12 | postgresql-pg-hba-rejects-connection |
| `password authentication failed for user` (28P01) | пароль, срок `VALID UNTIL` или md5 против scram-sha-256 | 12 | postgresql-password-authentication-failed |
| `connection to client lost`, `could not receive data from client` | клиент пропал: перезапуск сервиса, сеть, таймаут простоя у балансировщика | 3 | postgresql-connection-to-client-lost |
| `57P01 terminating connection due to administrator command` | сессию закрыли: перезапуск, переключение, `pg_terminate_backend` | 14 | rx-57p01-terminating-connection |
| `the database system is in recovery mode` (57P03), `terminated by signal 9` | процесс базы упал (чаще всего убит по памяти), сервер восстанавливается | 14 | postgresql-database-system-is-in-recovery-mode |
| `The remote certificate is invalid according to the validation procedure` | сервис RX не доверяет сертификату базы: корня нет в контейнере, имени нет в SAN, цепочка неполная | 12 | tls-remote-certificate-is-invalid |

## Транзакции и блокировки

| Текст | Что значит | Шаг | Статья |
|---|---|---|---|
| `deadlock detected` (40P01) | две транзакции ждали друг друга, одну откатили | 6 | postgresql-deadlock-detected-40p01 |
| `still waiting for ShareLock on transaction` | запрос дольше `deadlock_timeout` ждёт строку | 5 | postgresql-still-waiting-for-lock |
| `canceling statement due to lock timeout` (55P03) | не дождался блокировки за `lock_timeout` | 5 | postgresql-canceling-statement-lock-timeout |
| `terminating connection due to idle-in-transaction timeout` (25P03) | сессия молчала в открытой транзакции дольше таймаута | 4 | postgresql-idle-in-transaction-session-timeout |
| `current transaction is aborted, commands ignored` (25P02) | следствие: ищите первую ошибку этой сессии | 4 | postgresql-current-transaction-is-aborted |
| `Не удалось установить блокировку`, `is locked` | блокировка RX, а не PostgreSQL | 5 | rx-ne-udalos-ustanovit-blokirovku |
| `была изменена в другой транзакции` | два процесса RX сохранили одну запись почти одновременно | 5 | rx-record-changed-in-another-transaction |

## Запросы

| Текст | Что значит | Шаг | Статья |
|---|---|---|---|
| `could not execute query` | обёртка NHibernate, причина во внутреннем исключении | 11 | rx-could-not-execute-query |
| `canceling statement due to statement timeout` (57014) | запрос шёл дольше `statement_timeout` | 11 | postgresql-canceling-statement-due-to-statement-timeout |
| `canceling statement due to user request` | запрос отменил клиент по своему таймауту или администратор | 11 | postgresql-canceling-statement-user-request |
| `out of shared memory`, `max_locks_per_transaction` | переполнена таблица блокировок, часто при `pg_dump` базы с тысячами таблиц | 13 | postgresql-out-of-shared-memory |
| `ERROR: out of memory`, `Failed on request of size` | процессу не хватило памяти: `work_mem` на много соединений, тяжёлый запрос, лимит контейнера | 13 | postgresql-error-out-of-memory |
| `could not resize shared memory segment ... No space left on device` | в контейнере мал `/dev/shm` | 13 | postgresql-could-not-resize-shared-memory-segment |
| `relation ... does not exist` (42P01), `column ... does not exist` (42703) | не та база или схема, либо обновление RX применилось не полностью | 11 | postgresql-relation-does-not-exist, postgresql-column-does-not-exist |
| `duplicate key value violates unique constraint` (23505) | одновременная вставка или отставшая последовательность после восстановления | 11 | postgresql-duplicate-key-unique-constraint |
| `violates foreign key constraint` (23503) | удаляют или меняют запись, на которую ссылаются | 11 | postgresql-foreign-key-violation |

## WAL, диск, реплики

| Текст | Что значит | Шаг | Статья |
|---|---|---|---|
| `PANIC: could not write to file "pg_wal/..."`, `No space left on device` | кончилось место, база остановилась | 7 | postgresql-no-space-left-pg-wal |
| `archive command failed with exit code` | сегмент не ушёл в архив, `pg_wal` растёт | 8 | postgresql-archive-command-failed |
| `requested WAL segment ... has already been removed` | реплика отстала, нужный сегмент удалён | 9 | postgresql-requested-wal-segment-removed |
| `could not connect to the primary server` | реплика не достучалась до ведущего | 9 | postgresql-could-not-connect-to-primary |
| `recovery is in progress`, `WAL control functions cannot be executed during recovery` | функцию ведущего вызвали на реплике (обычно мониторинг) | 9 | postgresql-recovery-is-in-progress |
| `canceling autovacuum task` | autovacuum уступил конфликтующей блокировке | 10 | postgresql-canceling-autovacuum-task |
| `database is not accepting commands to avoid wraparound data loss`, `must be vacuumed within` | счётчик транзакций у предела | 10 | postgresql-wraparound |
| `WAL segment ... is absent` (pg_probackup) | дыра в архиве WAL | скил `rx-backup-check` | pg-probackup-wal-segment-is-absent |
