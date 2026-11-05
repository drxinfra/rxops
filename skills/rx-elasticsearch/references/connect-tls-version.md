# Связь RX с Elasticsearch, TLS, версия

## Отдельный сервер

Пересказ статьи drxinfra.ru «Elasticsearch на отдельном сервере для Directum RX»
(https://drxinfra.ru/blog/rx-elasticsearch-separate-server.html) и справки RX 25.2:

- `ELASTICSEARCH_URL` вида `http://адрес:9200` или адрес прокси `https://es.company.ru:443`. При установке
  через Directum Launcher адрес отдельного сервера указывают в параметре установки Elasticsearch, иначе
  там останется localhost.
- С серверов, где работают IndexingService и веб-сервер, адрес должен открываться:
  `curl -s -u <логин> <ELASTICSEARCH_URL>/_cluster/health` (curl спросит пароль) или `es_check.py`.
- Если Elasticsearch открыт в сеть, вендор рекомендует закрыть порт 9200 и поставить перед ним nginx
  с TLS и аутентификацией; тогда в `ELASTICSEARCH_URL` адрес прокси. Сертификат прокси от своего центра
  сертификации должен быть доверенным на серверах RX, иначе соединение падает на проверке цепочки.
- У рабочей и тестовой систем разные `ELASTICSEARCH_INDEX_POSTFIX` (а лучше разные кластеры): иначе они
  пишут в одни индексы. `es_check.py` показывает, сколько систем видно по именам индексов.
- `ELASTICSEARCH_SHOW_QUERIES` включают только на время разбора: журнал запросов быстро растёт. Имя
  файла по справке `<Имя компьютера>.<Имя сервиса>.ElasticsearchQuery.<Дата>.log`.
- Пустой `ELASTICSEARCH_URL` значит, что ничего не индексируется; после возврата адреса нужно
  первоначальное индексирование.

## TLS и пароль

У Elasticsearch 8 защита и TLS на HTTP-порту включены по умолчанию. Проверено на 8.19.5:

| Что видно | Где | Причина и что делать |
|---|---|---|
| `received plaintext http traffic on an https channel, closing connection Netty4HttpChannel{... remoteAddress=/<адрес клиента>:...}` | журнал Elasticsearch | клиент ходит по `http://` на порт с TLS; в `remoteAddress` адрес клиента (сервис индексирования, экспортёр, сборщик журналов). Поменять адрес клиента на `https://` и дать ему корневой сертификат (`config/certs/http_ca.crt` в образе 8.x) или поставить прокси |
| `Unexpected plaintext alert received: Level: fatal; Alert: unknown_ca` внутри `caught exception while handling client http traffic` | журнал Elasticsearch | клиент не доверяет сертификату Elasticsearch: корень не установлен на стороне клиента (у сервисов RX внутри контейнеров) |
| обрыв соединения на `http://`, `es_check.py` подсказывает https | клиент | то же, со стороны клиента |
| HTTP 401 | клиент | нет или неверные логин и пароль; у RX `ELASTICSEARCH_LOGIN` и `ELASTICSEARCH_PASSWORD` задаются парой |
| HTTP 403 | клиент | у учётки нет прав на операцию |

Не отключайте проверку сертификата, чтобы «заработало», и не выключайте TLS на самом Elasticsearch:
лучше прокси, который принимает HTTP во внутренней сети и ходит к Elasticsearch по HTTPS.

Проверка `ELASTIC_SEARCH` у IndexingService становится degraded, если Elasticsearch отвечает дольше
2000 мс: смотрите сеть и нагрузку узлов, а не только доступность
(`admin_unikalnyye_proverki_dlya_servisov_linux.htm`).

## Версия

- Для полнотекстового поиска вендор поддерживает только Elasticsearch 7.17.23; версии 7.17.13, 7.16.3,
  7.4.2 и 7.4.0 рекомендует обновить (`admin_elasticsearch_update_linux.htm`). Directum Launcher ставит
  образ `registry.directum.ru/public/elasticsearch:7.17.23` из комплекта поставки (`admin_install_es_dl.htm`).
- Под поиск RX вендор рекомендует сервер с 8 ядрами, 16 ГБ памяти и SSD, а индексы в среднем в 3–10 раз
  меньше суммарного объёма документов (типовые требования, пересказ калькулятора drxinfra).
- Elasticsearch 8.x и OpenSearch для поиска RX вне поддержки; кластер журналов к RX не привязан, там 8.x
  годится.
- Обновление с 7.17.13 до 7.17.23 по справке (пакетная установка): остановить Elasticsearch и Kibana,
  снять плагины, поставить пакеты 7.17.23, поставить плагины из комплекта поставки (analysis-morphology
  и ingest-attachment), запустить, проверить поиск в RX. Восстановление индексов после запуска может
  идти долго, поэтому обновление делают в нерабочее время. Это изменение, по шагам из справки своей
  версии и с согласия.
- Предупреждения при старте `TIFFImageWriter not loaded`, `J2KImageReader not loaded`,
  `sqlite-jdbc is not loaded`, `SLF4J: Defaulting to no-operation logger` пишет модуль извлечения текста
  (Apache Tika) про необязательные форматы. Если обычные документы ищутся по тексту, это не проблема;
  исключите их из тревог по журналам.
