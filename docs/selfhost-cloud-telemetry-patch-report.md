# Отчёт о патче отключения PostHog Cloud telemetry

## Общая информация

- Сообщение коммита: `feat(selfhost): disable cloud telemetry and billing limits`
- Область действия: основной server runtime и оба Hobby installer

Цель патча — прекратить автоматическую отправку эксплуатационной аналитики
самого PostHog в PostHog Cloud. При этом существующие вызовы аналитического
SDK должны завершаться как успешные no-op операции, чтобы их вызывающий код не
получал сетевых ошибок, не запускал повторные попытки и не останавливал основную
работу приложения.

## Центральная политика

Основное решение находится в `posthog/cloud_utils.py`. Вместо независимых
проверок переменных окружения в разных местах введена единая функция
`is_posthog_cloud_egress_enabled()`. Она всегда возвращает `False`.

У политики нет runtime-флагов и разрешённых режимов. `CLOUD_DEPLOYMENT`,
`DEBUG`, `SELF_CAPTURE`, `E2E_TESTING` и `OPT_OUT_CAPTURE` не могут включить
отправку. Helper сохранён как единая точка для существующих no-op веток и
будущих проверок egress.

## Как обеспечивается «положительный ответ»

Патч не подменяет HTTP-ответы централизованного сервера. Вместо этого он
останавливает отправку раньше, на уровне клиента или вызывающей функции:

| Тип вызова                         | Поведение после патча                                                    |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `posthoganalytics.capture(...)`    | Отключённый SDK принимает вызов и возвращает `None` без сети             |
| Отдельный экземпляр `Posthog(...)` | Создаётся с `disabled=True`; его capture-вызовы становятся no-op         |
| Celery-задача отправки отчёта      | Завершается сразу без исключения и без retry                             |
| Billing read                       | Возвращает локальные unlimited features и пустой каталог продуктов       |
| Billing update                     | Завершается без действия                                                 |
| Management command                 | Возвращается до выполнения HTTP-запроса                                  |
| Browser SDK без ключа              | Создаётся локальный отключённый объект, чтобы API SDK оставался доступен |
| Installer telemetry                | Функции остаются вызываемыми, но ничего не делают                        |

Это сохраняет ожидаемый интерфейс для остального кода, но не создаёт ложные
HTTP-ответы и не маскирует произвольные сетевые ошибки.

## Подробное обоснование изменений

### 1. Общий Python SDK

#### `posthog/cloud_utils.py`

Добавлена единая функция политики egress, которая всегда возвращает `False`.
Её используют backend, HTML context, middleware, management commands и фоновые
задачи. Включить отправку конфигурацией нельзя.

#### `posthog/apps.py`

В `AppConfig.ready()` глобальный клиент `posthoganalytics` безусловно переводится
в режим `disabled`. Удалены E2E и local-development ветки, которые включали
отдельный ключ, self-capture, загрузку feature flags или событие запуска dev
server. Через глобальный клиент проходят многочисленные `capture`,
`group_identify`, `alias` и `capture_exception` по всему Python-коду.

Это изменение закрывает большую часть telemetry одной точкой, не требуя правок
в каждом месте вызова. Отключённый клиент сохраняет API SDK и возвращает
управление вызывающему коду без отправки события.

#### `posthog/asgi.py`

Удалён ASGI wrapper, который после Django startup повторно инициализировал
self-capture и мог снять глобальный `disabled`. Основное ASGI application и
task-run ingest wrapper продолжают запускаться без дополнительного шага.

#### `posthog/ph_client.py`

В репозитории есть вызовы, которые создают новый `Posthog`, а не используют
глобальный клиент из `posthog/apps.py`. Новый экземпляр не наследует глобальное
поле `disabled`.

`get_client()` принудительно устанавливает `disabled=True`. Даже аргумент
`disabled=False` от отдельного вызывающего кода не может включить отправку.

### 2. Серверные usage reports и команды

#### `posthog/tasks/usage_report.py`

Изменены два независимых канала:

1. `get_ph_client()` всегда создаёт отключённый отдельный SDK-клиент;
2. `send_report_to_billing_service()` завершается до HTTP POST в billing service,
   если cloud egress запрещён.

Первое изменение закрывает события общего usage report. Второе необходимо,
потому что billing usage отправлялся напрямую через `requests`, в обход SDK.

#### `posthog/tasks/ai_observability_usage_report.py`

AI observability report создавал собственный синхронный `PostHogClient`.
Клиент теперь всегда получает `disabled=True`. Это
предотвращает синхронный сетевой запрос, сохраняя штатный вызов методов клиента.

#### `ee/tasks/send_license_usage.py`

Периодическая задача отправки license usage теперь возвращается до запросов к
`license.posthog.com`. Для планировщика это обычное успешное завершение задачи:
исключения и повторные попытки не возникают.

#### `posthog/management/commands/notify_helm_install.py`

Команда продолжает собирать и печатать локальный отчёт, но не включает SDK
обратно и не отправляет событие установки Helm.
Локальная диагностическая часть команды при этом сохранена.

#### `posthog/management/commands/sync_feature_flags_from_api.py`

Команда раньше запрашивала внутренние feature flags с `us.i.posthog.com`.
Теперь она всегда успешно завершается до запроса. Это также исключает загрузку
управляемых PostHog внутренних флагов в инстанс.

### 3. Billing paths

#### `ee/billing/billing_manager.py`

Все сетевые операции `BillingManager` заменены локальными реализациями:

- `get_billing()` читает сохранённые у организации features и возвращает нулевые
  расходы за текущий календарный месяц;
- `update_billing()` становится успешным no-op;
- `update_available_product_features()` сохраняет и возвращает локальный unlimited
  профиль;
- `_get_products()` возвращает пустой список продуктов;
- invoices, coupons и usage/spend возвращают пустые коллекции в ожидаемом формате;
- подписки, trials, смена плана и покупка credits завершаются без платежей и без
  изменения доступных возможностей;
- проверка авторизации возвращает завершённый `status="success"`, а portal URL
  ведёт на локальную страницу `/organization/billing`;
- Signals refund возвращает `credit_amount_usd="0"`, чтобы задача завершилась
  без повторных попыток; реальные денежные credits не создаются;
- funding status не заявляет наличие предоплаченных credits, а webhook завершается
  без пересылки данных провайдеру;
- входящие billing-данные не перезаписывают организацию и не понижают её features.

Используются структуры, которые уже понимал вызывающий код. Поэтому frontend и
фоновые задачи получают валидный результат без необходимости имитировать объект
`requests.Response`.

Эта правка означает, что UI не получает удалённый каталог продуктов,
тарифов и дополнений PostHog Cloud.

В `ee/api/billing.py` удалены ранние неполные ответы без лицензии для invoices,
credits, portal и авторизации: эти endpoints используют локальный manager.
Активация ключа проверяет формат входных данных и возвращает успех без HTTP и без
создания лицензии. Проверки membership и API scopes остаются на месте.
Отмена trial сохраняет прежний HTTP 200 с пустым телом; результат void-метода
manager не присваивается переменной. Схема API остаётся прежней.

#### Локальный unlimited entitlement-профиль

Профиль объединяет backend и технические frontend feature keys. Ключи договоров,
платной поддержки и обучения исключены: локальный код не предоставляет эти услуги. Обычные
feature-проверки всегда находят Paths Advanced, Group Analytics, white
labelling, SAML, SCIM, access control, audit logs, approvals, alerts,
subscriptions, surveys, replay и остальные возможности текущей версии.

У alerts, subscriptions и других числовых billing-квот `limit=None`, что в
существующих проверках означает отсутствие тарифного лимита. Activity Logs при
таком значении не получают fallback-ограничение глубины истории. Managed reverse
proxy получает практически недостижимый технический максимум вместо тарифа на
две записи.

Отдельный слой ingestion-квот хранится в Redis и используется Python, Node.js и
Rust-сервисами. Локальные Python-проверки отвечают «не ограничено» без чтения
Redis, а оба billing quota updater завершаются без изменения Redis, базы,
downstream-кэшей или Celery queue. Общая Node.js quota service и Rust
`RedisLimiter` сразу игнорируют только billing quota keys; отдельный
capture-overflow limiter в Rust продолжает работать. Поэтому чистая установка не
создаёт quota или suspension entries при старте. Event-driven refresh для
Self-driving также завершается до расчёта и записи квоты.

Retention ограничен только форматами, которые поддерживает само приложение:

- Session Replay получает максимальные доступные 5 лет;
- Logs получает предусмотренные продуктом 30 дней;
- Product Analytics retention не применяется на self-hosted, потому что его
  enforcement включается только для Cloud.

Единственный источник доступных возможностей — сохранённое поле
`Organization.available_product_features`. Профиль записывается при создании
организации через существующий `pre_save`. Подмен в `from_db()`, сериализаторе,
Team и User нет: Python, API и прямой SQL читают одинаковые данные.

Часовая задача обновляет отличающиеся профили, не записывая повторно уже актуальные
значения. `BillingManager.update_available_product_features()` использует тот же
метод модели с `save=True`. На существующей установке профиль обновляется командой
`sync_available_features` либо очередным запуском часовой задачи, а не чтением модели.

Node.js quota service сохраняет публичный интерфейс, но не создаёт загрузчик,
не читает Redis и не загружает Team ради проверки billing-квоты. Методы очистки
кэша сохранены как no-op для совместимости вызывающего кода.

Локальная коммерческая лицензия больше не определяет доступность функций. Она
может оставаться источником прочих license metadata, но наличие, тип и срок
лицензии не выключают product features.

### 4. HTML context и browser SDK

#### `posthog/utils.py`

Template context всегда сообщает `opt_out_capture=true`. Context ни в одном
режиме не содержит централизованные
`js_posthog_api_key`, `js_posthog_host` и `js_posthog_ui_host`.

E2E сохраняет свой несетевой marker, но больше не добавляет browser credentials.
Это не позволяет frontend восстановить cloud endpoint из серверного bootstrap.

Async `initialize_self_capture_api_token()` сохранён для существующих Dagster и
служебных вызовов, но стал успешным no-op. Он больше не меняет `disabled`, API key,
host или feature-flag provider.

#### `posthog/views.py`

Endpoint preflight теперь публикует то же effective-состояние opt-out, что и
template context. Frontend видит реальную политику инстанса, а не только исходное
значение одной переменной окружения.

#### `frontend/src/layout.html`

#### `frontend/src/layout.ejs`

Inline loader PostHog JS удалён. HTML не загружает `array.js` и не выполняет
`posthog.init()` против централизованного host.

Правка внесена в оба варианта layout, чтобы старый и текущий пути сборки не
расходились.

#### `frontend/src/loadPostHogJS.tsx`

Некоторые части frontend ожидают, что объект PostHog JS существует даже при
отключённой аналитике. Поэтому вместо полного отказа от инициализации создаётся
локальный no-op клиент с фиктивным токеном и следующими ограничениями:

- `api_host` указывает на origin самого self-hosted инстанса;
- `advanced_disable_decide=true` запрещает запрос feature flags;
- `disable_external_dependency_loading=true` запрещает загрузку внешних
  зависимостей SDK;
- `opt_out_capturing_by_default=true` отключает capture до инициализации;
- `autocapture=false`, а callback дополнительно вызывает `opt_out_capturing()`.

Так frontend сохраняет совместимый объект SDK, но он не отправляет события и не
обращается к PostHog Cloud.

#### `frontend/src/scenes/onboarding/legacy/sdks/hooks/useAdblockDetection.ts`

Adblock detection содержал прямой probe на
`https://us.i.posthog.com/decide/`, то есть обходил общую инициализацию SDK.

Теперь при отсутствии `window.JS_POSTHOG_HOST` hook возвращает `ok` без fetch.
Если host явно настроен, проверяется именно он, а не жёстко заданный US endpoint.
Возврат `ok` не показывает пользователю ложное предупреждение об adblocker в
режиме, где аналитика отключена намеренно.

#### `posthog/api/user.py`

Toolbar redirect больше не добавляет параметры `instrument`, `userEmail` и
`distinctId`, если cloud egress запрещён. Это предотвращает включение внутренней
toolbar instrumentation и передачу идентификатора пользователя через этот путь.

#### `posthog/middleware.py`

CSP middleware больше не добавляет ни в одном режиме:

- `report-uri https://us.i.posthog.com/report/...`;
- директиву `report-to posthog`;
- HTTP-заголовок `Reporting-Endpoints` с PostHog Cloud URL и `distinct_id`.

Остальная Content Security Policy сохраняется. Браузер продолжает применять CSP,
но нарушения и browser crash reports не уходят в централизованный сборщик.

#### `products/surveys/backend/llm/client.py`

#### `products/product_tours/backend/llm/client.py`

Удалены debug-ветки, которые повторно устанавливали
`posthoganalytics.disabled=False` перед созданием Gemini client. Вызов Gemini
остаётся рабочей функцией продукта, но связанный PostHog observability client
остаётся отключённым.

### 5. Hobby installers

#### `bin/deploy-hobby`

Удалены два прямых `curl` POST на `us.i.posthog.com/batch/`: событие начала и
событие успешного окончания установки. Эти запросы выполнялись независимо от
Django, поэтому общая Python-политика на них не влияла.

Удаление не меняет установку, health check или exit code скрипта.

#### `bin/hobby-installer/core/telemetry.go`

Go installer создавал отдельный PostHog client и отправлял те же install events.
Публичные функции `SendInstallStartEvent`, `SendInstallCompleteEvent` и
`CloseTelemetry` сохранены, но стали пустыми.

Сохранение сигнатур позволяет остальному installer-коду продолжать вызывать их
без условных веток и без ошибки. Зависимость `posthog-go` не удалялась из
dependency-файлов, чтобы не смешивать изменение lock-файлов с функциональным
патчем; после этого изменения runtime её не использует данный файл.

### 6. Документация

#### `docs/selfhost-upstream-update-checklist.md`

Чеклист описывает обязательную проверку запрета telemetry после обновления
upstream: поиск новых клиентов, регрессионные тесты и наблюдение за сетевыми
попытками в изолированной установке. Правило закреплено в `AGENTS.md`.

## Обоснование тестовых изменений

### `posthog/test/test_run_mode.py`

Добавлена таблица режимов, проверяющая, что egress запрещён в Cloud, local,
Hobby и при явном `OPT_OUT_CAPTURE=true`.

### `posthog/test/test_ph_client.py`

Проверяется, что `disabled=False` не может обойти запрет даже в Cloud mode и что
вызов `capture()` отключённого клиента возвращает `None`.

### `posthog/test/test_get_context_for_template.py`

Проверяется, что обычный и E2E template context содержат effective opt-out и не
содержат ключ или host PostHog Cloud.

### `posthog/test/test_utils.py`

Добавлен дешёвый no-DB тест, закрепляющий no-op контракт async self-capture
initializer. Удалены тесты выдачи browser token для self-capture. Такой token
больше не выдаётся ни в одном режиме, поэтому прежние тесты закрепляли удалённое
поведение.

### `posthog/test/test_middleware.py`

Добавлен тест отсутствия `Reporting-Endpoints` и cloud `report-uri` даже в Cloud
mode. Тесты разрешённой ветки удалены, потому что такой ветки больше нет.

### `posthog/tasks/test/test_usage_report_clients.py`

Новый тест проверяет оба отдельных usage-report клиента и отсутствие прямого
HTTP POST в billing usage task.

### `ee/billing/test/test_billing_manager.py`

Тесты локальных операций запрещают HTTP на границе `requests.Session` и проверяют
совместимые ответы с лицензией и без неё. Устаревшие тесты отправки запросов,
повторных попыток и применения облачных тарифов удалены вместе с соответствующим
поведением. Проверки общих JWT-helper сохранены.

### `ee/api/test/test_organization.py` и `posthog/api/test/test_organization.py`

Тесты проверяют запись профиля при создании организации, отсутствие подмен при
чтении и сериализации, совпадение прямого чтения БД и API после синхронизации,
а также отсутствие повторных UPDATE при неизменном профиле.
Существующий тест создания второй организации без лицензии теперь ожидает успех:
локальный профиль включает эту возможность. Проверки прав участников сохранены.

### `posthog/api/test/test_preflight.py` и `posthog/api/test/test_proxy_record.py`

Ожидаемые ответы учитывают безусловный `opt_out_capture=true`, доступность
multi-org без лицензии и создание третьей proxy-записи без прежнего free-tier
ограничения. Остальные проверки API сохранены.

### `posthog/api/advanced_activity_logs/test_utils.py`

No-DB тест проверяет, что feature без числового лимита не получает скрытое
двухмесячное ограничение истории.

### `ee/billing/test/test_quota_limiting.py`

Тест помещает контрольные quota и suspension entries в Redis, вызывает оба quota
updater и проверяет, что состояние Redis не изменилось, LLM generation key не
создан, а результат остаётся «не ограничено». Старые тесты удалённого
billing-контракта явно включают legacy policy только внутри теста.

### Node.js и Rust limiter tests

Node.js тест закрепляет, что проверки не обращаются к Redis или TeamManager
и не блокируют ingestion даже после вызова методов очистки кэша.
Rust тесты проверяют выключенные billing keys отдельно от действующего
capture-overflow limiter.

### `ee/tasks/test/test_send_license_usage.py`

Добавлен тест успешного no-op без HTTP. Существующие тесты формата license
telemetry явно подменяют policy внутри теста и не выполняют реальную сеть.

### `frontend/src/loadPostHogJS.test.ts`

Новый тест проверяет, что при отсутствии ключа создаётся клиент с local origin,
выключенными decide/external dependencies, autocapture и capture по умолчанию.

### `frontend/src/scenes/onboarding/legacy/sdks/hooks/useAdblockDetection.test.ts`

Новые тесты проверяют две ветки:

- отсутствие analytics host возвращает `ok` без fetch;
- явно заданный host используется вместо жёстко заданного US endpoint.

## Выполненные проверки

7 сентября 2026 года окружение Flox и зависимости восстановлены. Успешно выполнены:

- `uv run mypy --cache-fine-grained .`: 19 337 исходных файлов без ошибок;
- `hogli ci:preflight --strict`: без ошибок, включая Python lint/format,
  Markdown, lock-файлы, workflows и проверку конфликтов миграций;
- `hogli build:openapi`: схема и сгенерированные файлы не требуют изменений;
- Jest: `loadPostHogJS`, `useAdblockDetection` и Node.js quota limiter — 4 теста;
- Go: `go test ./core/...` для Hobby installer;
- Rust: `cargo test -p limiters -- redis` — 9 тестов;
- Backend: 203 целевых теста SDK, usage reports, billing, CSP, template context,
  организаций и отсутствия записей billing-квот;
- Дополнительный backend-прогон: 190 тестов полного billing API, preflight,
  proxy records, audit-log lookback и self-capture initializer; наборы частично
  пересекаются;
- `bash -n bin/deploy-hobby` и `git diff --check`.

Для preflight использованы и база fork-патча, и стандартное сравнение с
`origin/master`. Отдельная проверка Desktop пропущена из-за отсутствия его
изолированного Node-окружения; fork-патч Desktop не изменяет.

Backend-тесты используют локальные Postgres, ClickHouse, Kafka, Redis и SeaweedFS.
Для CSP-тестов подготовлены пустые HTML-шаблоны по образцу backend CI; это не
проверка собранного frontend.
Дополнительный прогон выполнялся с `WIZARD_CLOUD_RUN_OAUTH_CLIENT_ID=`, чтобы
локальная OAuth-конфигурация не меняла ожидаемый preflight-ответ.

Полные Django/frontend suites и наблюдение за сетью работающего браузера и
серверных workers не выполнены: перечисленные
проверки сами по себе не подтверждают отсутствие всех попыток исходящих соединений.

## Границы патча

Патч отключает автоматическую operational telemetry основного server runtime и
двух Hobby installers во всех режимах. Он не является универсальным сетевым
firewall.

Сознательно не блокируются:

- destinations, которые администратор сам настроил для отправки клиентских
  данных, например HTTP batch export;
- внешние интеграции продуктов, включая email, Slack, warehouse sources и
  настроенных LLM-провайдеров;
- build-time и административные загрузки, например GeoLite или документация;
- отдельные продукты и сервисы со своим runtime и конфигурацией, включая
  PostHog Desktop, mobile app, MCP UI apps и standalone LLM gateway.

Это важно: утверждение «инстанс вообще не делает исходящих соединений» нельзя
гарантировать только этим коммитом.

## Риски и эксплуатационные последствия

1. Внутренние PostHog feature flags больше не синхронизируются из Cloud.
   Код должен использовать свои default-ветки.
2. Browser CSP violations и crash reports больше не видны PostHog.
3. Billing UI не получает каталог cloud-тарифов, но product paygates открыты
   локальным entitlement-профилем; billing quota updater не изменяет хранилища.
4. Новая upstream-функция может создать собственный HTTP или SDK client и обойти
   текущую политику, если не использует общий helper.
5. Новые backend feature keys попадут в профиль автоматически, но новый
   frontend-only `AvailableFeature` нужно добавить в локальный список при merge с
   upstream.
6. Неполный интеграционный прогон оставляет риск несовместимости с более широким
   Django/frontend окружением.

## Рекомендации для строгого air-gap режима

Для гарантии отсутствия любых соединений с PostHog Cloud следует применять
защиту в несколько слоёв:

1. Запретить домены PostHog Cloud через DNS, firewall, egress proxy или container
   network policy.
2. Использовать allowlist разрешённых внешних destinations вместо общего доступа
   контейнеров в интернет.
3. Логировать заблокированные egress-попытки на сетевом уровне, чтобы находить
   новые обходные пути после обновления upstream.
4. После каждого merge с upstream повторять поиск прямых `Posthog(...)`,
   `requests.*`, `fetch(...)` и жёстко заданных доменов PostHog.
5. Перед развёртыванием запустить полный Django, frontend и Hobby smoke-test в
   подготовленном Flox-окружении.

## Итог

Коммит безусловно отключает автоматическую PostHog Cloud telemetry, закрывает
известные обходные каналы и сохраняет безопасные no-op контракты для вызывающего
кода. Для абсолютной сетевой изоляции этот патч нужно использовать вместе с
инфраструктурным egress deny.
