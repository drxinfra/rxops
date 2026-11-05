# Диск, блокировки записи, red и yellow

Все команды с PUT и POST меняют кластер: только с согласия, с объяснением и откатом. В примерах
`curl -u <логин>` спросит пароль сам; для https добавьте `--cacert <ca.crt>`.

## Пороги диска

Значения по умолчанию (проверено в `_cluster/settings` на 7.17.23):

| Порог | По умолчанию | Что делает узел |
|---|---|---|
| `cluster.routing.allocation.disk.watermark.low` | 85 % | новые шарды на узел не размещает |
| `...watermark.high` | 90 % | уводит шарды с узла на другие |
| `...watermark.flood_stage` | 95 % | закрывает индексы узла на запись (read_only_allow_delete) |

Порог задают процентом занятого места или объёмом свободного (`100gb` значит «свободно меньше 100 ГБ»).
Справка RX упрощает: по умолчанию сервис работает, пока занято не больше 85 %
(`admin_nastroyka_indeksov_elasticsearch_linux.htm`). Наблюдение за диском ставят на 80 %, до первого
порога (калькулятор drxinfra).

```bash
curl -s -u <логин> "$ES_URL/_cat/allocation?v"
curl -s -u <логин> "$ES_URL/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.disk"
curl -s -u <логин> "$ES_URL/_all/_settings/index.blocks.*?flat_settings=true"
```

## Индексы закрыты на запись

Проверено на 7.17.23. Узел выше flood_stage пишет в журнал
`flood stage disk watermark [...] exceeded on [...] ..., all indices on this node will be marked read-only`,
а запись в индекс получает HTTP 429:
`index [<индекс>] blocked by: [TOO_MANY_REQUESTS/12/disk usage exceeded flood-stage watermark, index has read-only-allow-delete block]`.
В старых версиях текст `FORBIDDEN/12/index read-only / allow delete (api)`. Ручная блокировка записи
выглядит как `FORBIDDEN/8/index write (api)` (HTTP 403). Для RX это значит: новые и изменённые документы
не попадают в поиск, сервис индексирования получает ошибки, очередь индексирования в брокере растёт.

Порядок:

1. Освободить место или расширить диск. Удаление индексов (например старых журналов в общем кластере)
   это тоже изменение и только с согласия: удалённый индекс не вернуть.
2. С версии 7.4 блокировка снимается сама, когда занятость опускается ниже high. На 7.17.23 это видно в
   журнале: `releasing read-only-allow-delete block on indices: [[<индекс>]]`. Ждать долго не нужно,
   проверка идёт примерно раз в полминуты.
3. Если блокировка осталась (старая версия или поставлена вручную), снять её (изменение):
   ```bash
   curl -s -u <логин> -X PUT "$ES_URL/<индекс>/_settings" -H 'Content-Type: application/json' \
     -d '{"index.blocks.read_only_allow_delete": null}'
   ```
   Отката не требует: при нехватке места Elasticsearch поставит блокировку снова. Блокировку `write` или
   `read_only`, поставленную кем-то намеренно, без выяснения не снимайте.
4. Проверить, что очередь индексирования в RabbitMQ разбирается (скил `rx-rabbitmq`), а число документов
   в индексах RX растёт (`es_check.py --interval 300`).

## Red и yellow

```bash
curl -s -u <логин> "$ES_URL/_cat/indices?v&health=red"
curl -s -u <логин> "$ES_URL/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason"
curl -s -u <логин> "$ES_URL/_cluster/allocation/explain?pretty"
```

Red: хотя бы один основной шард не размещён, его данные недоступны, поиск в RX неполный или с ошибкой
«all shards failed». Yellow: основные шарды на месте, не хватает реплик. Allocation explain без тела
объясняет случайно выбранный неназначенный шард и прямо называет причину. Типовые:

- **Узел выпал.** Вернуть узел, шарды поднимутся сами.
- **Порог диска.** Освободить место, затем повторить размещение (изменение):
  `curl -s -u <логин> -X POST "$ES_URL/_cluster/reroute?retry_failed=true"`.
- **Размещение выключено.** `cluster.routing.allocation.enable` не `all` (так оставляют после
  обслуживания узлов). Вернуть значение по умолчанию (изменение):
  `curl -s -u <логин> -X PUT "$ES_URL/_cluster/settings" -H 'Content-Type: application/json' -d '{"persistent":{"cluster.routing.allocation.enable":null}}'`.
- **Фильтр размещения** (`index.routing.allocation.require` и подобные) не совпадает ни с одним узлом:
  explain пишет `node does not match index setting`. Выяснить, кто и зачем его ставил.
- **Шард повреждён, копии нет.** Индекс придётся пересоздать и переиндексировать. Для индексов RX это
  штатная операция, документы в самой RX не теряются (`references/indexing-reindex.md`).

Yellow на одном узле из-за реплик: разместить реплику на том же узле, где основной шард, нельзя (explain:
`same_shard`). Кластер так и будет жёлтым, к этому привыкают и пропускают настоящий red. Для одного узла
реплики ставят в ноль (изменение):

```bash
curl -s -u <логин> -X PUT "$ES_URL/<индекс или _all>/_settings" -H 'Content-Type: application/json' \
  -d '{"index.number_of_replicas": 0}'
```

Откат: то же с прежним числом. Новые индексы создаются с настройками из шаблона: у RX индексы строит
InitialIndexing по своим шаблонам (`document_template.json`, `workflow_template.json`, справка
`admin_nastroyka_indeksov_elasticsearch_linux.htm`). Задано ли там число реплик, проверьте на своей версии
и после переиндексации посмотрите `_cat/indices` ещё раз.
