# Настройки безопасности самой Directum RX

Что проверить в RX помимо серверов. Сверено со справкой Directum RX 25.2, в скобках имя статьи для
`rx_help_topic`. В другой версии имена параметров и значения по умолчанию могут отличаться: если подключён
rxmcp, откройте ту же статью в справке своей версии. Параметры config.yml читайте через маскировщик:
`python3 ${CLAUDE_SKILL_DIR}/scripts/mask_secrets.py /srv/DirectumLauncher/etc/config.yml`
(путь из примеров справки, у вас может быть другой).

Всё ниже только смотрим. Изменение настроек делается в Directum Launcher или в config.yml с применением
настроек и перезапуском сервисов: это изменение системы, только по плану и с согласия.

## Вход в систему

| Что | Где смотреть | На что обратить внимание | Статья |
|---|---|---|---|
| Тип аутентификации | справочник «Учётные записи», параметр `EXTERNAL_AUTHENTICATION_TYPE` в `common_config` | `Kerberos` работает только внутри домена; рекомендуемый протокол OpenID Connect 1.0; SAML 2.0 и WS-Federation справка называет устаревшими | `admin_nastroika_autentifikatcii_linux.htm` |
| Политики учётных записей | модуль «Администрирование», «Пользователи и группы», «Политики учетных записей» | для входа по паролю: в политике по умолчанию блокировка после 15 неудачных попыток на 10 минут; значения по умолчанию для пароля: длина от 6, журнал хешей 3; длину стоит поднять. Справочник видят роли «Администраторы» и «Аудиторы» | `sungero_coreentities_loginpolicy.htm` |
| Блокировка по IP | `IP_BLOCKING_MAX_FAILED_ATTEMPTS`, `IP_BLOCKING_LOCKOUT_DURATION_MINS`, `IP_BLOCKING_FAILED_ATTEMPTS_RESET_AFTER_MINS` в секциях веб-сервера и сервиса интеграции | по умолчанию выключена; действует и для сервиса интеграции | `admin_nastroika_blokirovki_ip_adresov_linux.htm` |
| Адрес клиента за прокси | `FORWARDED_HEADERS_FORWARD_LIMIT`, `FORWARDED_HEADERS_KNOWN_PROXIES`, `FORWARDED_HEADERS_KNOWN_NETWORKS` | без них блокировку по IP и адреса в истории можно подменить заголовком; доверять заголовкам только от своих прокси | та же статья |
| Второй фактор | группа `MultifactorAuthentication`: `MFA_ENABLED`, `MFA_UNSUPPORTED_CLIENT_ACTION` | второй фактор по сертификату на токене; в сервисе интеграции и мобильных приложениях не поддерживается: `Exception` (по умолчанию) запрещает там вход, `Skip` пускает по одному фактору | `admin_2fa_certificate.htm` |
| Оповещение о прошлом входе | `PREVIOUS_LOGON_NOTIFICATION_ENABLED` в секции `SungeroWebServer` | при `true` пользователь видит дату прошлого входа и может заметить чужой | `ar_model_bezopasnosti.htm` |
| Неактивные сеансы | фоновый процесс «Завершение сеансов неактивных пользователей» | по умолчанию выключен; если включён, раз в час завершает сеансы без активности больше 120 минут | `admin_zavershenie_seansov_polzovatelei.htm` |
| Подключение к домену | `LDAP_SETTINGS` в `common_config`: `useSsl`, `sslServerCertificateThumbprint` | без `useSsl: true` пароли пользователей идут к контроллеру домена открыто | `inst_ldap_linux.htm` |
| Блокировка в домене | порог неудачных попыток в домене | справка советует порог с запасом: при входе бывает до четырёх попыток подключения | `admin_nastroika_autentifikatcii_linux.htm` |

Вход через Keycloak (OpenID Connect) и Kerberos внутри сети снаружи с кодом из приложения разобраны
в статье https://drxinfra.ru/blog/rx-sso-kerberos-ldaps-2fa.html. Каталог подключается к Keycloak только
по LDAPS, консоль администрирования Keycloak снаружи закрыта.

## Служебные учётки

- **Administrator.** Служебный пользователь, пароль задаётся при установке. Справка рекомендует свести
  работу под ним к минимуму или закрыть его и работать от своего администратора в роли «Администраторы»
  (`admin_pricladnye_nastroiki.htm`).
- **Integration Service.** Если интеграция с 1С не используется, справка советует закрыть эту учётную
  запись (поле «Состояние» = «Закрытая», та же статья).
- **Service User.** Служебный пользователь сервисов: `AUTHENTICATION_USERNAME` (по умолчанию `Service User`)
  и `AUTHENTICATION_PASSWORD` в `common_config` (`admin_minimalnye_nastroiki_linux.htm`).
- **Учётка в базе.** `User ID` в `CONNECTION_STRING`. Для PostgreSQL работающему пользователю достаточно
  роли с атрибутом login; суперпользователь нужен, только если та же учётка создаёт базу. Для Microsoft SQL
  Server справка не рекомендует `sa` (та же статья). При установке из командной строки для создания базы
  есть отдельный администратор `CONNECTION_STRING_ADMIN_USERNAME` (`inst_ustanovka_s_komandnoi_stroki_linux.htm`).

## Секреты в config.yml

- Файл настроек: `DirectumLauncher/etc/config.yml` (`admin_nastr_config_linux.htm`). В нём строки подключения
  к базе и брокеру с паролями, пароль служебного пользователя, пароли LDAP, SMTP, Elasticsearch, секреты
  провайдеров входа. По нему генерируются файлы `*_ConfigSettings.xml` сервисов, в них те же значения.
- Права: файл и каталог должны читать только root и учётка, от которой работает Launcher. Проверка:
  `host_audit.py --only secrets --config-dir /srv/DirectumLauncher/etc`.
- Шифрование параметров: в секции `variables` переменная `config_password` со ссылкой на переменную
  окружения, затем `./do.sh enc encrypt_config` и перегенерация `./do.sh installer config_up`,
  `./do.sh all config_up` (`admin_shifrovanie_parametrov.htm`). Шифруются только параметры RX из списка
  в статье (полный список: `./do.sh sungero_deploy.schema show`); секции сторонних компонентов, например
  RabbitMQ (`SungeroRabbitMQ`), остаются открытыми, поэтому пароли в них должны отличаться. Это изменение
  конфигурации: только по плану, на тестовом стенде сначала. После шифрования Directum Launcher запускают
  из той же сессии с заданной переменной окружения, пароль шифрования хранят отдельно.
- Сертификат шифрования cookie и проверки токенов: `DATA_PROTECTION_CERTIFICATE_FILE` и
  `DATA_PROTECTION_CERTIFICATE_FILE_PASSWORD` в `common_config`, один и тот же на всех узлах
  (`admin_cluster_directumrx.htm`). Файл сертификата с закрытым ключом не должен быть доступен всем.

## Directum Launcher

- По умолчанию страница открывается на `http://127.0.0.1:5000/`, порт меняется ключом `--port`
  (`admin_nastr_launcher_linux.htm`).
- С ключом `--host=0.0.0.0` страница доступна с других компьютеров. Справка советует тогда ключи
  `--auth` (вход по имени и паролю, один пользователь, хранится в секции `launcher` config.yml) и `--https`
  (`inst_dop_param_linux.htm`). Без них любой, кто дошёл до порта, меняет настройки системы.
- Норма: Launcher не держат запущенным без нужды; если держат, то с `--auth` и `--https` и порт закрыт
  экраном. Проверка: `host_audit.py --only ports` (5000 на всех адресах) и `ps -eo user,args | grep -i [D]irectumLauncher`.

## Протокол и порты

- Переменная `protocol`: `https` по умолчанию, `http` только для среды разработки и ознакомления
  (`admin_minimalnye_nastroiki_linux.htm`).
- Порты по умолчанию (`inst_ports.htm`): веб-сервер 443 или 80, база 5432 или 1433, RabbitMQ 5672 и 15672,
  сервис хранилищ 10056, сервис интеграции 27002, сервис логирования 36801, хранение файлов предпросмотра
  28013, агент управления сервисами 46130, Centrifugo 48480, сервис клиентских подключений 20050,
  Elasticsearch 9200, Kibana 5601. Если порт занят, при установке берётся следующий свободный, поэтому
  фактические порты смотрите в config.yml и выводе `ports.list`.

## Журналы и история

- История действий с объектами (закладка «История» карточки) показывает, кто и когда работал с документом,
  и IP-адреса (`ar_model_bezopasnosti.htm`).
- Обращение служебных пользователей к зашифрованным документам и документам со строгим доступом пишется
  в журнал сервиса ошибкой `Access to encrypted entity or entity with strict access mode`
  (`admin_logirovanie_obrash_konfidentc_doc_linux.htm`). Появление таких записей нужно разбирать.
- Журналы сервисов лежат в `LOGS_PATH`, по умолчанию `<папка с данными>/logs`, журналы Launcher в
  `DirectumLauncher/log` (`admin_logs_linux.htm`). Сбор в одно место: скил `rx-logs`.
