# Ошибки RabbitMQ в логах RX и брокера

Пересказ справочника ошибок drxinfra.ru и справки RX 25.2. Подробные страницы: https://drxinfra.ru/errors/
(имя страницы в скобках).

## Где искать

- Логи сервисов RX лежат в папке из `LOGS_PATH` в config.yml (в примере справки `{{ home_path }}/logs`,
  `admin_server_linux.htm`). Ищите по тексту ошибки и времени жалобы:
  `grep -rl 'recovery failed' <папка логов> | head`.
- Журнал брокера: у пакетной установки обычно `/var/log/rabbitmq/rabbit@<узел>.log`, у контейнера
  `docker logs --since 1h <контейнер>`.
- Кто подключается чаще всех (шторм подключений), по журналу брокера. Формат строк проверен на 3.13.7:
  ```bash
  grep -h 'authenticated and granted access' /var/log/rabbitmq/rabbit@*.log \
    | sed -E 's/.*\(([^ ]+) -> [^ ]+ - ([^)]*)\): user .([^ ]+). .*/\1 \2 \3/; s/^([0-9a-fA-F.:]+):[0-9]+ /\1 /' \
    | sort | uniq -c | sort -rn | head
  ```
  В выводе адрес клиента, имя соединения (у сервисов RX в нём имя сервиса) и учётка. Клиент без имени
  соединения в эту выборку не попадёт, тогда считайте строки `accepting AMQP connection` по адресу.

## Ошибки в логах сервисов RX

| Текст | Что значит | Что проверить |
|---|---|---|
| `BrokerUnreachableException: None of the specified endpoints were reachable` | клиент перебрал все адреса брокера и не подключился; настоящая причина во вложенном исключении ниже (rabbitmq-broker-unreachable-exception) | строки после `--->` или `Inner Exception`: `Connection refused` (брокер не запущен, не тот порт), `ACCESS_REFUSED`, ошибка сертификата, таймаут; затем `nc -vz <брокер> 5672`, `rabbitmq-diagnostics listeners` |
| `RabbitMQ is not available. Possible problems: TLS configuration (check useTls setting). Incorrect port` | признак TLS и порт не совпадают с брокером (5672 без TLS, 5671 с TLS) или брокер недоступен (rx-rabbitmq-not-available-tls) | `nc -vz <брокер> 5672`, `nc -vz <брокер> 5671`, `openssl s_client -connect <брокер>:5671 -servername <брокер> </dev/null \| head -20`, `references/tls.md`. Имя и формат параметра TLS в строке подключения сверьте со справкой своей версии: в справке 25.2 его нет |
| `RabbitMQ connection '...' shutdowned`, `... recovery failed`, `FailedAutoRecovery` | соединение оборвалось, автоматическое восстановление не удалось; сервис может остаться без брокера и при этом считаться запущенным (rx-rabbitmq-connection-recovery-failed) | работает ли брокер (`rabbitmq-diagnostics check_running`), есть ли после восстановления брокера новые записи о подключении в логе сервиса; нет: перезапуск сервиса с согласия. Справка советует при долгой недоступности брокера проверить и перезапустить сервисы (`admin_ustranenije_oshibok_v_log_failah_linux.htm`) |
| `Error occurred while publishing JobMessage` (DelayOperationMessage, QueueHealthCheckMessage), `The message acknowledgment by RabbitMQ has timed out` | брокер не подтвердил приём вовремя; сообщение могло дойти, а могло потеряться (rx-rabbitmq-publish-timeout) | тревоги (шаг 4), `rabbitmqctl cluster_status`, задержка диска брокера `iostat -x 1 3`, совпадение с перезапуском узлов или массовой операцией |
| `RabbitMQ channel is failed to close due to timeout.`, `RabbitMQ operation timeout [CreateQueue...]`, `Cannot recreate consumer for '...' queue` | брокер медленно отвечает на операции с каналами (rx-rabbitmq-channel-close-timeout) | единичные при перезапуске брокера или сервиса: фон; тысячи в сутки: число каналов и соединений, диски и ресурсы узлов, кворумные очереди на медленном диске |
| `Already closed: The AMQP operation was interrupted` | канал или соединение уже закрыты; причина в хвосте сообщения (rx-already-closed-amqp-operation-interrupted) | `initiated by Peer` с `PRECONDITION_FAILED ... delivery acknowledgement timed out`: долгий обработчик, `references/alarms-limits.md`; `CONNECTION_FORCED`: брокер перезапускали или соединение закрыл администратор; без кода: сеть и таймауты простоя |
| `Attempts count to execute async handler ... has exceeded` | асинхронный обработчик исчерпал попытки, работа не выполнена (rx-async-handler-attempts-exceeded) | исходная ошибка раньше в логе Worker с тем же именем обработчика; брокер тут обычно ни при чём |
| `Keep-alive failed` с `Connection refused` | сервис клиентских подключений не достучался до Centrifugo (rx-keep-alive-failed) | это не RabbitMQ, хотя симптом похож («новые задания видны только после обновления страницы»): Centrifugo и его порт |
| `Multiple exceptions: [Errno 10061] Connect call failed ('127.0.0.1', 5672)` | сервисы Directum Ario не нашли брокер по адресу из настроек (ario-rabbitmq-connect-call-failed) | `rabbitmq-diagnostics listeners`, адрес брокера в настройках Ario |

## Ошибки в журнале брокера

| Текст | Что значит | Что проверить |
|---|---|---|
| `ACCESS_REFUSED - Login was refused using authentication mechanism PLAIN` | неверные имя или пароль, guest не с localhost, нет прав на vhost (rabbitmq-access-refused-login-was-refused) | `rabbitmqctl list_users`, `rabbitmqctl list_permissions -p <vhost>`; совпадают ли имя, пароль и vhost с `QUEUE_CONNECTION_STRING` (регистр важен, справка `admin_minimalnye_nastroiki_linux.htm`) |
| `PRECONDITION_FAILED - delivery acknowledgement on channel N timed out` | потребитель держал сообщение дольше `consumer_timeout`, брокер закрыл канал и вернул сообщение (rabbitmq-delivery-acknowledgement-timed-out) | очередь названа в предупреждении перед ошибкой: `Consumer '...' on channel N and queue '<очередь>' in vhost '<vhost>' has timed out waiting for a consumer acknowledgement` (текст 3.13.7); затем `rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers` и `references/alarms-limits.md` |
| `PRECONDITION_FAILED - inequivalent arg 'x-queue-type' for queue ...` | очередь уже есть с другими параметрами, сервис не стартует (rabbitmq-precondition-failed-inequivalent-arg) | не смотрит ли на брокер второй стенд с тем же exchange; меняли ли тип очередей; `rabbitmqctl list_queues name type arguments durable \| grep <очередь>`. Непустую очередь не удалять |
| `operation queue.declare caused a channel exception not_found: no queue ...` | клиент проверил очередь, которой нет (rabbitmq-queue-declare-not-found) | для временных очередей фон; пропала постоянная очередь: кто удалил, пересоздаёт ли её сервис при старте |
| `failed consuming from quorum queue ... noproc` | у кворумной очереди нет процесса на узле (rabbitmq-quorum-queue-noproc) | `references/cluster-quorum.md` |
| `no case clause matching ... in ra_log_wal:open_existing` | повреждён WAL кворумных очередей после аварийной остановки, узел не стартует (rabbitmq-ra-log-wal-crash) | `references/cluster-quorum.md` |
| `client unexpectedly closed TCP connection` | клиент пропал без закрытия AMQP (rabbitmq-client-unexpectedly-closed-tcp) | пачка при перезапуске клиентов: норма; обрывы с одинаковым интервалом: таймаут простоя балансировщика или сетевого экрана, heartbeat меньше этого таймаута |
| `disk resource limit alarm set on node`, `memory resource limit alarm set on node` | брокер заблокировал публикацию (rabbitmq-disk-resource-limit-alarm, rabbitmq-memory-resource-limit-alarm) | шаг 4, `references/alarms-limits.md` |
| `Deprecated features: management_metrics_collection` | предупреждение 3.13 при старте о сборе метрик в API управления | не авария; метрики для мониторинга лучше брать из плагина rabbitmq_prometheus (порт 15692) |
