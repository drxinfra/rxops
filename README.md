# rxops: скилы для администраторов Directum RX

<img src="assets/icon.svg" width="88" height="88" align="right" alt="rxops">

**Agent Skills for Directum RX operations.** A Claude Code plugin with skills for administrators of Directum RX (Sungero): incident triage, logs and a base of ~200 known errors, PostgreSQL, RabbitMQ, Elasticsearch, updates with rollback, backup verification, security self-assessment, sizing, and connecting the agent to RX help via [rxmcp](https://github.com/drxinfra/rxmcp). Read-only by default; hooks ask before state-changing commands and mask secrets before the model sees them. Content is in Russian.

Набор скилов для ИИ-агента, который помогает администратору Directum RX: разобрать инцидент, найти ошибку в логах, проверить базу, брокер, поиск и бэкап, спланировать обновление, оценить защищённость и посчитать железо. Скил — это инструкция для агента: что смотреть, какими командами, что считать нормой и куда идти дальше. Агент подгружает нужный скил сам, когда тема вопроса совпала с его описанием.

Основа — статьи и справочник ошибок [drxinfra.ru](https://drxinfra.ru), проверенные на настоящих PostgreSQL, RabbitMQ и Elasticsearch, и справка Directum RX той версии, что стоит у вас (через rxmcp).

## Что внутри

| Скил | Когда срабатывает |
|---|---|
| `rx-incident` | «RX лежит», «всё тормозит», «у пользователей ошибки»: первые 15 минут, сбор фактов, передача в нужный скил |
| `rx-logs` | где лежат логи RX, как их читать, поиск известных ошибок по базе из 200 с лишним разборов |
| `rx-postgresql` | соединения, блокировки, дедлоки, autovacuum, рост pg_wal и архив WAL, реплики, медленные запросы |
| `rx-rabbitmq` | очередь растёт, нет потребителей, тревоги по памяти и диску, осиротевшие очереди, TLS |
| `rx-elasticsearch` | красный кластер, блокировка записи по диску, куча и лимиты, индексирование документов RX |
| `rx-update` | обновление с тестовым контуром, бэкапом, проверками до и после и планом отката |
| `rx-backup-check` | что входит в бэкап RX, свежий ли он и восстанавливается ли на самом деле |
| `rx-security-check` | самооценка защищённости: опросник, проверки хоста, отчёт с красной, жёлтой и зелёной зонами |
| `rx-sizing` | серверы, диски и рост хранилища по нагрузке, настройки PostgreSQL, RabbitMQ и Elasticsearch |
| `rx-connect` | установка и проверка rxmcp, ответы по справке своей версии RX |

Внутри скилов есть скрипты проверок на Python 3.6+ и bash без внешних пакетов: в закрытых контурах на Astra Linux и РЕД ОС их можно запускать как есть.

## Безопасность

- **Сначала только чтение.** Скилы предписывают агенту сначала смотреть, а команду, которая что-то меняет, показывать и выполнять только после явного согласия. Скрипты проверок ничего не меняют.
- **Хук подтверждения.** Перед командами, которые меняют систему (`systemctl restart`, `docker compose down`, `kubectl apply`, запись в базу через `psql`, `rabbitmqctl purge_queue`, `curl -X DELETE`, удаление в системных каталогах, установка пакетов и т. п.), Claude Code спрашивает человека, даже в автоматическом режиме. Форматирование дисков и `rm -rf /` хук запрещает совсем.
- **Хук маскировки.** Вывод команд, прочитанные файлы и результаты поиска проходят через маскировщик до того, как их увидит модель: пароли, токены, ключи, строки подключения и учётки в адресах заменяются на `‹скрыто›`. Проверено в Claude Code 2.1: модель получает маску вместо значения.
- **Ограничения.** Хуки работают только в Claude Code и нужен `python3` в `PATH`; без него хуки не срабатывают, и защита держится только на инструкциях скилов. Проверка команд построена на шаблонах, и необычно записанную команду она может пропустить. В режиме полного обхода разрешений (`--dangerously-skip-permissions`) не полагайтесь на подтверждение. Маскировка действует на то, что уходит модели; если в организации включён экспорт телеметрии Claude Code, исходный вывод может попасть туда.
- Выключить хуки: `RXOPS_GUARD=off` (подтверждение) и `RXOPS_MASK=off` (маскировка) в переменных окружения или в `env` настроек Claude Code.

## Установка

**Claude Code** (рекомендуется: скилы и хуки):

```
/plugin marketplace add drxinfra/rxops
/plugin install rxops@drxinfra
```

То же из терминала: `claude plugin marketplace add drxinfra/rxops` и `claude plugin install rxops@drxinfra`. Скилы вызываются сами по теме вопроса или явно: `/rxops:rx-incident`.

**Без доступа к GitHub.** Скачайте `rxops-<версия>.zip` со страницы [Releases](https://github.com/drxinfra/rxops/releases) или с [drxinfra.ru/rxops.html](https://drxinfra.ru/rxops.html), сверьте сумму с `SHA256SUMS`, распакуйте и запускайте `claude --plugin-dir /путь/к/rxops`. Происхождение архива: `gh attestation verify <архив> --repo drxinfra/rxops`.

**Claude Desktop и claude.ai.** Загрузите нужные скилы архивами из `skills/` релиза (Settings → Capabilities → Skills). Хуков там нет: подтверждение и маскировка держатся на инструкциях скилов.

**Другие агенты.** Скилы написаны в открытом формате Agent Skills (папка с `SKILL.md`, см. [agentskills.io](https://agentskills.io)). Агент, который понимает этот формат, может взять папки из `skills/` как есть. Проверяли мы только в Claude Code.

## Как пользоваться

Пишите агенту, как написали бы коллеге:

- «С утра у всех тормозит веб-клиент RX, разберись, что происходит на сервере»
- «В логе `FATAL: sorry, too many clients already`, что это и что делать»
- «Очередь в RabbitMQ растёт, задания не двигаются»
- «Проверь, что бэкап RX живой и восстановится»
- «Посчитай диски: 400 документов в день, средний файл 1,2 МБ, на 5 лет»

Агент работает там, где запущен: на сервере RX, на машине администратора с доступом к серверам по SSH или в контейнере с нужными клиентами (`psql`, `curl`, `kubectl`). Для ответов по справке своей версии RX подключите rxmcp, это делает скил `rx-connect`.

## Разработка

- Как устроен скил и правила оформления: [docs/authoring.md](docs/authoring.md).
- Проверки: `python3 tools/check_skills.py`, `python3 -m unittest discover -s tests`, `sh tools/leakcheck.sh`.
- Общий маскировщик живёт в `shared/`, копии в скилах и хуках раскладывает `python3 tools/sync_shared.py`.
- Справочник ошибок для `rx-logs` собирается из статей сайта: `python3 tools/sync_errors.py`.
- Архивы релиза: `python3 tools/package.py`.

## Лицензия

Apache 2.0, см. [LICENSE](LICENSE) и [NOTICE](NOTICE). Имена rxops и drxinfra лицензия не передаёт: форк распространяйте под другим именем.

Directum RX — продукт компании Directum. Проект с вендором не связан.
