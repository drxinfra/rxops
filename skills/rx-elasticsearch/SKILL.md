---
name: rx-elasticsearch
description: >-
  Диагностика Elasticsearch для полнотекстового поиска Directum RX: статус кластера, неназначенные шарды,
  диск и пороги watermark, блокировка read-only-allow-delete, куча JVM и circuit breakers, лимиты шардов
  и полей, версия 7.17.23, связь с сервисом индексирования и переиндексация. Использовать, когда «поиск
  не находит новые документы», «поиск ничего не находит», cluster health red или yellow, unassigned
  shards, «index has read-only-allow-delete block», flood-stage watermark, circuit_breaking_exception,
  «Data too large», java.lang.OutOfMemoryError: Java heap space, «maximum shards open», «Limit of total
  fields [1000] has been exceeded», immense term, «received plaintext http traffic on an https channel»,
  INDEXING_SERVICE degraded, ELASTIC_SEARCH unhealthy, ошибки IndexingServiceMessageHandler, Elasticsearch
  на отдельном сервере, нужна ли переиндексация. Очередь индексирования в брокере не разбирает: это скил
  rx-rabbitmq; записи в журналах сервисов ищет скил rx-logs.
license: Apache-2.0
compatibility: >-
  Directum RX 4.x–26.x; Elasticsearch 7.x и 8.x, OpenSearch (для поиска RX вендор поддерживает только
  7.17.23; скрипт проверен на 7.17.23 и 8.19.5 с TLS). Нужен python3 3.6+ и доступ к порту 9200 с
  учёткой, у которой есть право monitor.
metadata:
  author: drxinfra
  version: "0.1.0"
---

# Elasticsearch для поиска Directum RX

Скил разбирает поисковую систему, в которую сервис индексирования RX складывает документы, задачи и
задания: связь, версию, статус кластера, диск и блокировки, кучу и шарды, а ещё когда и как
переиндексировать. Очередь индексирования в RabbitMQ проверяет скил `rx-rabbitmq`.

## Правила

- Сначала только смотрим. Команду, которая что-то меняет (перезапуск, удаление, изменение настроек, запись в базу, очистку очереди), сначала покажите пользователю, объясните, что она сделает и как это откатить, и выполняйте только после явного согласия.
- Пароли, токены и ключи не выводите. Конфиги и вывод, где они могут быть, читайте через маскировщик: `python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py ФАЙЛ` или `КОМАНДА 2>&1 | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py`.
- Если подключён rxmcp (есть инструменты `rx_help_search` и `rx_help_topic`), поведение Directum RX сверяйте со справкой той версии, что стоит у пользователя, и называйте статью.
- Чего не знаете точно, так и говорите и предлагайте проверить на тестовом стенде. Не придумывайте параметры, пути и имена служб.
- `${CLAUDE_SKILL_DIR}` — папка этого скила. Если агент не подставляет переменную, скрипты лежат в `scripts/` рядом с этим файлом.

Для этого скила дополнительно:

- Индексы не удалять, `_reindex` и первоначальное индексирование с ключом `-r` не запускать без явного
  согласия: `-r` очищает очередь индексирования и удаляет индексы RX, поиск пустеет до конца перестройки.
- Каталоги данных Elasticsearch между серверами не копировать: индексы строят заново.

## Что известно об RX и Elasticsearch

По справке Directum RX 25.2 (статьи в скобках):

- Путь документа в поиск: веб-сервер кладёт запрос в очередь RabbitMQ `directumrx_<exchange>_indexing_service`,
  IndexingService забирает его, берёт содержимое у сервиса хранилищ и отправляет в Elasticsearch
  (`ar_servis_indeksirovaniia.htm`, `admin_indeksirovanie_dokumentov_linux.htm`).
- Настройки в `common_config` config.yml: `ELASTICSEARCH_URL` (пусто: ничего не индексируется и проверка
  здоровья не выполняется), `ELASTICSEARCH_INDEX_POSTFIX` (у рабочей, тестовой системы и среды разработки
  разные), `ELASTICSEARCH_INDEX_PREFIX`, `ELASTICSEARCH_LOGIN` и `ELASTICSEARCH_PASSWORD` (оба или ни
  одного), `ELASTICSEARCH_SHOW_QUERIES` (`admin_obshchie_nastroiki_linux.htm`).
- Индексы: `[префикс_]rxsearch_document_<код системы>_<постфикс>`, `rxsearch_workflowtask_...`,
  `rxsearch_workflowassignment_...`, для поиска по критериям `..._properties_...`
  (`admin_indeksirovanie_dokumentov_linux.htm`, `admin_nastroyka_polnotekst_poiska_linux.htm`).
- Поддерживается только Elasticsearch 7.17.23; 7.17.13, 7.16.3, 7.4.2 и 7.4.0 вендор советует обновить
  (`admin_elasticsearch_update_linux.htm`). Версия 8.x годится для кластера журналов, не для поиска RX.
- Проверки здоровья IndexingService: `ELASTIC_SEARCH` unhealthy при ошибке или без связи, degraded при
  ответе дольше 2000 мс; `INDEXING_SERVICE` degraded после 25 ошибок подряд, сервис перестаёт забирать
  очередь, пока обработка не наладится (`admin_unikalnyye_proverki_dlya_servisov_linux.htm`).

## Порядок работы

1. **Симптом.** Что не ищется: новые документы, всё или один документ; с какого времени; переносили
   ли Elasticsearch, восстанавливали ли RX из копии, меняли ли префикс или постфикс. Не находится один
   документ: сразу шаг 8. Перенос, восстановление, смена префикса: шаг 9.

2. **Куда ходит RX.** Пароль спрячет маскировщик:
   ```bash
   grep -n 'ELASTICSEARCH_' <папка Directum Launcher>/etc/config.yml | python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py
   ```
   Норма: адрес задан, постфикс есть, логин и пароль заданы парой или оба пусты.

3. **Снимок кластера.** Запускайте с сервера, где работает IndexingService, тем же адресом и учёткой:
   ```bash
   ES_URL=<ELASTICSEARCH_URL> python3 ${CLAUDE_SKILL_DIR}/scripts/es_check.py --netrc ~/.config/rxops/es.netrc
   ```
   Файл netrc (`machine <сервер ES> login <логин> password <пароль>`, права 600) пользователь заполняет сам,
   пароль агенту не нужен. Без пароля (поиск без безопасности) ключ `--netrc` не нужен.
   Для https добавьте `ES_CA=<корневой сертификат>`. Норма: «Итог: OK», код 0. Строки CRIT и WARN ведут в
   шаги ниже по имени проверки. Учётку только на чтение и ключи описывает `references/es-check.md`.

4. **Связь, TLS, версия** (ошибка скрипта, `version`). Отказ соединения, обрыв на http-адресе (TLS на
   порту), недоверенный сертификат и 401 скрипт объясняет сам; разбор и прокси перед Elasticsearch в
   `references/connect-tls-version.md`. Версия не 7.17.23 под индексами RX: это вне поддержки вендора,
   скажите пользователю прямо. На 7.x скрипт ещё ищет плагины analysis-morphology и ingest-attachment,
   которые справка ставит из комплекта поставки (`plugins`, `admin_elasticsearch_update_linux.htm`).

5. **Статус red или yellow** (`health`). Red: часть основных шардов не размещена, поиск неполный или с
   ошибкой «all shards failed». Причину скрипт берёт из allocation explain: выпал узел, порог диска,
   фильтр размещения. Yellow на одном узле из-за реплик лечится `number_of_replicas: 0` (изменение).
   `cluster.routing.allocation.enable` не `all`: так часто забывают после обслуживания. Разбор и команды
   в `references/disk-blocks.md`.

6. **Диск и блокировки** (`disk`, `blocks`). Пороги по умолчанию 85, 90 и 95 % занятого места. На 95 %
   (flood_stage) Elasticsearch закрывает индексы узла на запись: индексирование получает
   `cluster_block_exception ... TOO_MANY_REQUESTS/12/disk usage exceeded flood-stage watermark`, новые
   документы не ищутся, очередь индексирования растёт. Сначала место, потом блокировка: с 7.4 она
   снимается сама ниже порога high (на 7.17.23 в журнале «releasing read-only-allow-delete block on
   indices»). Команды, ручное снятие и проверка очереди после в `references/disk-blocks.md`.

7. **Куча, предохранители, шарды** (`heap`, `shards`). Высокая куча и сработавшие предохранители дают
   `circuit_breaking_exception` и `Data too large`, нехватка кучи роняет узел с `OutOfMemoryError`.
   Шарды у лимита дают `maximum shards open`. Ошибки полей: `Limit of total fields` и `immense term`.
   Размеры, ориентиры и команды в `references/heap-shards.md`.

8. **Индексирование.** Очередь `indexing_service` и её потребителей проверьте скилом `rx-rabbitmq`
   (`mq_check.py`). Затем лог сервиса индексирования (`IndexingServiceMessageHandler`,
   `Indexing completed with an error`, код ответа Elasticsearch 400, 429, 503), статус-файлы проверок
   `ELASTIC_SEARCH` и `INDEXING_SERVICE`, прирост документов: `es_check.py --interval 300`. Один документ
   проверяется запросом по его ИД. Порядок, типовые причины и запросы в `references/indexing-reindex.md`.

9. **Нужна ли переиндексация.** Нужна после переноса Elasticsearch на новый сервер, восстановления RX из
   копии, смены префикса или постфикса, после того как `ELASTICSEARCH_URL` был пуст, и при ошибках схемы
   индекса. Не нужна, если индексы закрылись из-за диска или кластер был red: чините Elasticsearch,
   очередь догонит сама. Утилита InitialIndexing, индексирование по периодам и по ИД, фоновые процессы
   контроля индексации и опасный ключ `-r` разобраны в `references/indexing-reindex.md`. Любой запуск
   индексирования только с согласия.

10. **После исправления.** Сохраните замер до работ (`es_check.py --json > before.json`), после сравните
    (`es_check.py --compare before.json`). Норма: green (или yellow на одном узле с пояснением), блокировок
    нет, очередь индексирования пустеет, число документов в индексах RX растёт, проверки
    `ELASTIC_SEARCH` и `INDEXING_SERVICE` в статус-файлах healthy.

## Что сказать пользователю

- **Что нашли.** Каждая проблема одной строкой с цифрами: статус, сколько шардов не размещено и
  почему, занятость диска и порог, какие индексы закрыты, куча, шарды к лимиту.
- **Насколько срочно.** Авария: red, индексы RX закрыты на запись, узел упал по памяти, у RX нет связи
  с Elasticsearch. Скоро: диск между 80 % и flood_stage, куча и шарды у пределов, предохранители.
  В план: yellow на одном узле, версия не 7.17.23, вход без пароля, журналы в одном кластере с поиском.
- **Что сделать.** Команды по порядку; каждую, что меняет систему, пометьте, объясните и дайте откат.
  Переиндексацию называйте отдельным решением с оценкой времени, а не шагом между делом.
- **Чем подтверждено.** Строки `es_check.py`, ответ allocation explain, строки журналов, статьи справки
  RX и drxinfra.ru.

## Справочник

- `references/es-check.md`: запуск `es_check.py`, учётка только на чтение, проверки и пороги, коды возврата.
- `references/connect-tls-version.md`: отдельный сервер, прокси и TLS, учётка, версия 7.17.23 и 8.x.
- `references/disk-blocks.md`: пороги watermark, блокировки и их снятие, red и yellow, allocation explain.
- `references/heap-shards.md`: куча JVM, предохранители, лимит шардов, лимит полей, immense term.
- `references/indexing-reindex.md`: путь индексирования, ошибки сервиса, проверка одного документа,
  переиндексация и фоновые процессы контроля.
- Статьи drxinfra.ru: [Elasticsearch на отдельном сервере](https://drxinfra.ru/blog/rx-elasticsearch-separate-server.html),
  [мониторинг RX и алерты](https://drxinfra.ru/blog/rx-monitoring-alerts.html),
  [логи RX и шарды](https://drxinfra.ru/blog/rx-logs-centralized.html),
  [калькулятор настроек Elasticsearch](https://drxinfra.ru/elasticsearch-config.html),
  [справочник ошибок](https://drxinfra.ru/errors/).
