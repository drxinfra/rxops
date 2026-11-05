# Ошибки обновления и публикации Directum RX

Тексты ошибок дословно, по справочнику https://drxinfra.ru/errors/ и справке Directum RX 25.2
(статья `admin_update_defects_linux.htm`). Ищите по куску текста из журнала. Журналы читайте через
маскировщик, команды из столбца «Что делать», которые что-то меняют, выполняйте по согласию.

Главное правило справки: **если ошибка возникла на этапе конвертации базы, сначала восстановите
базу из бэкапа, потом повторяйте обновление**, иначе при повторной попытке база станет нерабочей.

Где искать:

- `DirectumLauncher/log/current.log`: текущие сообщения, их же показывает страница Directum
  Launcher; прежние переносятся в `all.log`;
- `DirectumLauncher/log/dl.log`: ошибки при обновлении;
- `DirectumLauncher/log/DBConverter.log`: конвертация базы;
- `<LOGS_PATH>/<компьютер>.DeploymentToolCore.<дата>.log`: публикация разработки;
- журналы сервисов в `LOGS_PATH` и статус-файлы в `LOGS_PATH/status`.

`preflight.py --post --no-containers --launcher-dir <папка>` сам ищет в журналах Directum Launcher
тексты из этой таблицы и даёт ссылки.

## Directum Launcher и установка

| Текст ошибки | Что значит | Что делать | Разбор |
|---|---|---|---|
| `root action {component_name} already exists` | с 25.1 компонент directumrx называется base, а в папке остался архив `directumrx.tar.gz` или `.zip` прошлой версии | `./do.sh components add-all` (Windows `do components add-all`), удалить архив, повторить обновление | https://drxinfra.ru/errors/directum-launcher-root-action-already-exists.html |
| `Ошибка с кодом 503` | при обновлении: сервисы ещё не запустились; при установке на этапе публикации: сервисы в контейнерах не пускает к базе `pg_hba.conf` | при обновлении подождать и «Повторить»; при установке разрешить подсеть контейнеров в `pg_hba.conf` (реальную подсеть, а не `/0`) и `SELECT pg_reload_conf();` | https://drxinfra.ru/errors/directum-launcher-error-503.html |
| `Permission denied: '/var/tmp/_HostOsInfo'` | служебный файл остался от запуска под другим пользователем | удалить файл (`sudo rm /var/tmp/_HostOsInfo`) и повторить; запускать Directum Launcher всегда под одним пользователем | https://drxinfra.ru/errors/directum-launcher-permission-denied.html |
| `./Directum Launcher: Отказано в доступе` | нет прав на папку новой версии Directum Launcher или на исполнение файлов | выдать пользователю права на папку и все файлы в ней | там же |
| `Couldn't find a valid ICU package installed on the system` | .NET не нашёл библиотеку ICU | `export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1` и запуск в том же сеансе, или пакет libicu из репозитория ОС | https://drxinfra.ru/errors/directum-launcher-icu-package.html |
| `ERROR /opt/directum/ario/deploy.yml does not exist` | нет прав на запись в папку установки Ario или файла нет в сборке | выдать права; если файла нет в `_builds/Ario/ario_plugin/data/`, повторить с распаковки архива Ario | https://drxinfra.ru/errors/ario-deploy-yml-does-not-exist.html |
| `docker.errors.BuildError: ... returned a non-zero code: 1` | rootless Docker: SELinux с драйвером fuse-overlayfs или `USER_ID` пересекается с зарезервированными группами | драйвер overlay2, `USER_ID` вне занятых диапазонов; отключение SELinux только по согласованию с ИБ | https://drxinfra.ru/errors/docker-rootless-builderror-user-id.html |
| `Cannot create protocol "http" binding with port "80"` | Windows: порты 80 и 443 заняты другим сайтом IIS | остановить Default Web Site, ставить в него (`site_name` в секции IIS `config.yml`) или взять другие порты | https://drxinfra.ru/errors/iis-cannot-create-binding-port-80.html |
| страница Directum Launcher не загружается | старый кэш браузера | очистить кэш и жёстко перезагрузить страницу (справка) | `admin_update_defects_linux.htm` |

## Публикация разработки и настройки

| Текст ошибки | Что значит | Что делать | Разбор |
|---|---|---|---|
| DeploymentToolCore, код возврата 1–8 | этап, на котором упала публикация: 1 до отправки пакета, 2 передача или несовпадение версий утилиты и веб-сервера, 3 инициализация, 4 настройки по умолчанию, 5 и 6 импорт и экспорт настроек, 7 экспорт пакета, 8 номер версии | по коду искать ошибку в журнале утилиты; для 2 сверить версии, для 3 смотреть журнал веб-сервера; в конвейерах проверять код возврата | https://drxinfra.ru/errors/deploymenttoolcore-exit-codes.html |
| `Базовое соединение закрыто: Непредвиденная ошибка при приеме` | перекрыты абстрактные типы (например, входящий документ), а пакет экспортирован без базовых решений | экспортировать пакет с базовыми решениями и повторить публикацию | https://drxinfra.ru/errors/rx-publish-connection-closed-unexpected.html |
| `Метаданные на сервере отличаются от метаданных из пакета` | настройки импортируют в систему с другой прикладной разработкой | сначала выровнять разработку (опубликовать пакет исходной системы), потом импорт настроек | https://drxinfra.ru/errors/rx-settings-metadata-mismatch.html |
| `Ошибка обработки элемента. Тип сущности "Параметр процесса", имя сущности "". Не заполнено обязательное свойство "Имя"` | с 4.9 настройки по умолчанию нельзя строить на сущностях без внешних ссылок | донастроить в среде разработки, пересохранить варианты процессов и настройки по умолчанию | https://drxinfra.ru/errors/rx-default-settings-process-parameter-name.html |
| `'Region' - FAILED` после `--restore-fk` | при переходе с 4.6 и ниже ключ не восстановлен: строки ссылаются на удалённые записи | найти и исправить строки (пример запроса в разборе), повторить `--restore-fk` | https://drxinfra.ru/errors/rx-restore-fk-failed.html |

## После запуска сервисов

| Текст ошибки | Что значит | Что делать | Разбор |
|---|---|---|---|
| `Method not found: 'Int32 Sungero.Domain.Shared.IEntity.get_Id()'` | задачи отправили перед остановкой: сервисы обновились раньше прикладной разработки | после публикации и запуска сервисов возобновить прекращённые задачи; справка предлагает и способ избежать (временно закомментировать часть сервисов в `config.yml`) | `admin_update_defects_linux.htm` |
| `Method ... not found in type`, `MissingMethodException` | части разработки разных версий: публикация прошла не везде | переопубликовать пакет целиком, перезапустить сервисы; проверить, что все узлы на одной версии | https://drxinfra.ru/errors/rx-method-not-found-in-type.html |
| `Не удалось загрузить модуль из /app/: произошла ошибка` | модуль не инициализировался при старте; причина во вложенном исключении (RabbitMQ, база, пакет) | устранить причину и перезапустить сервис: загрузка модулей только при старте | https://drxinfra.ru/errors/rx-module-load-failed.html |
| `The constructor being called throws an exception` | следствие незагруженного модуля или разных версий сервисов | смотреть журнал сервиса с момента запуска, довести обновление до конца | https://drxinfra.ru/errors/rx-dependency-resolution-failed.html |
| `Failed to get deployment package status` | проверка работоспособности не достучалась до базы | разбирать недоступность базы (скил `rx-postgresql`), публикация обычно ни при чём | https://drxinfra.ru/errors/rx-deployment-package-status.html |
| `PRECONDITION_FAILED - inequivalent arg 'x-queue-type'` | очередь уже есть с другими параметрами, например после смены типа очередей | убедиться, что на брокер не смотрит второй стенд; дальше по разбору, скил `rx-rabbitmq` | https://drxinfra.ru/errors/rabbitmq-precondition-failed-inequivalent-arg.html |
| `Требуется наличие лицензии на модуль` | лицензия не загружена после обновления или переноса, истекла или не покрывает модуль | загрузить лицензию; на тестовом стенде проверить, та ли лицензия | https://drxinfra.ru/errors/rx-trebuetsya-nalichie-licenzii.html |
| `Health check result has been changed.` | у сервиса сменилось состояние проверки | смотреть, какая проверка отказала и как долго; алерт по самой проверке, а не по строке | https://drxinfra.ru/errors/rx-health-check-result-has-been-changed.html |
| `INDEXING_SERVICE degraded`, `ELASTIC_SEARCH unhealthy` | индексирование не работает или Elasticsearch не отвечает | восстановить зависимость; новые документы не попадают в поиск (скил `rx-elasticsearch`) | https://drxinfra.ru/errors/rx-healthcheck-degraded-statuses.html |
| `Клиент с ИД ... не зарегистрирован на сервере` | сеансы сброшены перезапуском | массово после перезапуска это фон; пользователю войти заново | https://drxinfra.ru/errors/rx-client-not-registered.html |
| `Cannot read properties of undefined` в журнале веб-клиента | в браузере осталась старая версия клиента или ошибка компонента | обновить страницу с очисткой кэша; если повторяется, собрать пример и передать владельцу компонента | https://drxinfra.ru/errors/rx-webclient-javascript-error.html |
