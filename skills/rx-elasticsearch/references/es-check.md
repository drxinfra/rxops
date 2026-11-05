# es_check.py: запуск, учётка, проверки

Скрипт делает только GET-запросы: `/`, `_cluster/health`, `_cat/nodes`, `_cat/allocation`,
`_cluster/settings`, `_cat/indices`, `_all/_settings/index.blocks.*`, `_nodes/stats`, `_nodes`,
`_cat/plugins`, а при неназначенных шардах ещё `_cat/shards` и `_cluster/allocation/explain` (GET без тела
объясняет первый попавшийся неназначенный шард). Ничего не меняет.

## Запуск

```bash
ES_URL=https://es01:9200 ES_CA=/путь/к/ca.crt python3 es_check.py --netrc ~/.config/rxops/es.netrc
```

- `ES_URL` (по умолчанию `http://localhost:9200`), `ES_USER`, `ES_PASSWORD`, `ES_CA` или ключи `--url`,
  `--user`, `--cacert`; файл в формате netrc: `--netrc ФАЙЛ`. Без имени скрипт ходит без пароля.
- `--insecure` не проверяет сертификат и пишет об этом в заголовке; только осознанно.
- `--interval 300` второй замер через 5 минут: на сколько выросло число документов в индексах RX.
- `--json > before.json`, потом `--compare before.json`: сравнение с сохранённым замером.

Запускайте с сервера, где работает IndexingService, тем же адресом и той же учёткой, что в
`ELASTICSEARCH_URL` и `ELASTICSEARCH_LOGIN`: так проверяется и сеть, и доверие к сертификату, и права.

Ошибки связи скрипт объясняет (код 2): отказ соединения, обрыв на `http://` (на порту TLS), сертификат не
прошёл проверку (нужен `ES_CA`), 401 (нет или неверные имя и пароль), 403 (мало прав).

## Проверки

| Проверка | CRIT | WARN |
|---|---|---|
| `version` | | не 7.17.23 при индексах RX: старая 7.x, 8.x, OpenSearch |
| `plugins` | | на 7.x при индексах RX не видно analysis-morphology или ingest-attachment |
| `health` | red | yellow; `cluster.routing.allocation.enable` не `all`; очередь задач мастера от 50 |
| `disk` | диск выше high или flood_stage | выше low или больше 80 %; пороги выключены |
| `blocks` | read_only_allow_delete, read_only, write на индексе RX, блокировка кластера | другие блокировки |
| `heap` | | куча от 85 %, parent от 90 %, сработавшие предохранители, отклонённые запросы, куча больше половины памяти или 31 ГБ |
| `shards` | от 95 % лимита | от 80 % лимита; больше 20 шардов на гигабайт кучи |
| `rx` | красный индекс RX | основной шард индекса RX больше 30 ГБ |

INFO: закрепление памяти, список индексов RX и число систем (префикс, код системы, постфикс), чужие
индексы в кластере, ответ без пароля, прирост документов, что прочитать не удалось.

Предохранители и отклонения считаются с запуска узла: после исправления цифры не обнулятся, смотрите,
растут ли они (`--compare`). Куча в один момент может быть высокой до сборки мусора: повторите замер.

Код возврата: 0 норма, 1 найдено отклонение, 2 не удалось проверить.

## Учётка только на чтение

Если защита включена, заведите отдельную роль (изменение, только с согласия). Проверено на 8.19.5: этой
роли хватает на все запросы скрипта.

```bash
curl -s -u elastic --cacert <ca.crt> -X POST "$ES_URL/_security/role/rx_monitor" \
  -H 'Content-Type: application/json' \
  -d '{"cluster":["monitor"],"indices":[{"names":["*"],"privileges":["monitor","view_index_metadata"]}]}'
curl -s -u elastic --cacert <ca.crt> -X POST "$ES_URL/_security/user/rxmon" \
  -H 'Content-Type: application/json' -d '{"password":"<пароль>","roles":["rx_monitor"]}'
```

`curl -u elastic` спросит пароль сам. Откат: `DELETE _security/user/rxmon` и `DELETE _security/role/rx_monitor`.
Защиту в 7.17 включает `xpack.security.enabled: true` в elasticsearch.yml; справка RX упоминает базовую
аутентификацию и параметры `ELASTICSEARCH_LOGIN`, `ELASTICSEARCH_PASSWORD`
(`admin_nastroyka_polnotekst_poiska_linux.htm`).
