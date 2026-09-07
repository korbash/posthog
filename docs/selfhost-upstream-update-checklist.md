# Проверка автономного режима после обновления PostHog

Эта проверка обязательна после каждого `git merge`, включая merge без конфликтов
и fast-forward. Она также применяется после rebase, cherry-pick и обновления SDK,
если изменения затрагивают исходящие соединения. Отсутствие конфликтов не означает,
что новые пути отправки автоматически покрыты патчем.

Цель: собственная телеметрия и billing-данные инстанса не отправляются в PostHog
Cloud, вызывающий код получает ожидаемый локальный результат, а сбор пользовательских
событий и настроенные пользователем интеграции продолжают работать.

Текущая реализация описана в [отчёте о патче](selfhost-cloud-telemetry-patch-report.md).
Используйте его как карту, но сверяйте поведение с кодом после обновления.

## Обязательная настройка SDK на вашем сайте

Для событий **вашего сайта**, а не внутренней телеметрии интерфейса PostHog,
обязательно задайте `api_host` при инициализации браузерного SDK:

```js
posthog.init('YOUR_SELF_HOSTED_PROJECT_TOKEN', {
  api_host: 'https://analytics.example.com',
})
```

Замените пример на публичный HTTPS-адрес ingestion вашего self-hosted инстанса
либо своего reverse proxy, который направляет запросы в этот инстанс. Токен проекта
тоже должен быть из этой установки. Без явного `api_host` установленный сейчас
`posthog-js` использует `https://us.i.posthog.com`: события не попадут в ваш PostHog.
`ui_host` и серверный `SITE_URL` не заменяют `api_host`. Наш no-op патч внутренней
телеметрии не меняет настройки SDK на стороннем сайте.

После развёртывания и обновления SDK или конфигурации проверьте значение в
собранном frontend сайта, включая переменные окружения сборки. В браузере убедитесь,
что SDK-запросы идут в ваш ingestion/proxy без перенаправления в Cloud; отдельно
проверьте `flags_api_host` и `asset_host`, если они переопределены. Отправьте
синтетическое событие и подтвердите его появление в нужном локальном проекте:
одного HTTP 200 недостаточно. Не объявляйте сбор настроенным без этой проверки.

## 1. Зафиксировать границы обновления

Перед разрешённым пользователем merge запишите SHA текущего HEAD и входящего коммита.
Не начинайте merge поверх незакоммиченных правок без согласованного способа их
сохранения; не делайте автоматический stash, commit или reset пользовательской работы.

Пример для обновления из уже полученного `upstream/master`:

```sh
telemetry_pre_merge=$(git rev-parse HEAD)
telemetry_incoming=$(git rev-parse upstream/master)
git diff --name-status "$telemetry_pre_merge...$telemetry_incoming"
```

После merge сравните прежний HEAD с фактическим рабочим деревом, включая разрешение
конфликтов и последующие исправления:

```sh
git diff --name-status "$telemetry_pre_merge"
git diff "$telemetry_pre_merge" -- posthog ee frontend nodejs rust products services bin
git diff --check
```

Если merge уже выполнен, восстановите прежний SHA по reflog и проверьте его вручную.
Не используйте `HEAD^` или `ORIG_HEAD` вслепую: fast-forward и последующие операции
могут изменить смысл этих ссылок. Сам checklist не разрешает merge, commit или push.

## 2. Проверить изменившиеся точки отправки

Сначала изучите входящий diff и изменения lock-файлов SDK. Затем выполните поиск
по рабочему дереву: новый клиент может появиться вне файлов нашего патча.

```sh
rg -n -i 'posthoganalytics|posthog-js|posthog-node|posthog-go|posthog\.init|PostHogClient|Posthog\(' posthog ee frontend nodejs rust products services bin
rg -n -i 'posthog\.com|posthog\.net|BILLING_SERVICE_URL|Reporting-Endpoints|report-uri|report-to|OPT_OUT_CAPTURE|SELF_CAPTURE' posthog ee frontend nodejs rust products services bin
rg -n 'requests\.(get|post|patch|put|request)|httpx\.|fetch\(|sendBeacon|NewWithConfig|reqwest|disabled\s*=\s*False|opt_in_capturing' posthog ee frontend nodejs rust products services bin
```

Поиск даёт кандидатов, а не доказательство отсутствия отправок. Проследите адрес,
создание клиента и вызывающий код для новых или изменившихся путей, включая адреса
из конфигурации. Документационные ссылки, загрузки при сборке и явно настроенные
пользователем destinations оценивайте отдельно; не блокируйте их по одному слову
`posthog` в имени.

Обязательные точки проверки:

| Область                                                                            | Что должно сохраниться                                                                             |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `posthog/apps.py`, `posthog/asgi.py`, `posthog/utils.py`                           | Startup и self-capture не включают SDK повторно                                                    |
| `posthog/ph_client.py`, оба usage-report клиента                                   | Новые экземпляры SDK тоже отключены; переданный `disabled=False` не обходит запрет                 |
| `ee/billing/billing_manager.py`, `ee/api/billing.py`, задачи usage/license reports | Ни прямые HTTP-запросы, ни новые методы не обходят локальную реализацию                            |
| `frontend/src/loadPostHogJS.tsx`, оба layout, onboarding adblock detection         | Нет загрузки облачного SDK, capture, decide/flags, probes или дополнительных зависимостей из Cloud |
| `posthog/middleware.py`, toolbar в `posthog/api/user.py`                           | Не возвращаются адреса отправки CSP/crash reports и параметры облачной инструментации              |
| Hobby installers и management commands                                             | Нет отправки install/dev/Helm telemetry и импорта внутренних flags из Cloud                        |
| Новые сервисы, фоновые workers, SDK и их зависимости                               | Отдельные процессы и новые транспорты проверены независимо от Django-клиента                       |

Проверяйте сигнатуры и ответы: capture может возвращать `None`, но billing-потребитель
может ожидать список, `status`, сумму или URL. Универсальный `200 {}` не гарантирует
совместимость. Локальные URL не должны перенаправлять на Cloud или зацикливаться.

## 3. Запустить целевые регрессионные тесты

Все команды окружения запускайте из корня через `.codex/with-flox`.
Минимальный набор после каждого merge:

```sh
.codex/with-flox hogli test posthog/test/test_ph_client.py posthog/test/test_run_mode.py posthog/tasks/test/test_usage_report_clients.py ee/billing/test/test_billing_manager.py ee/api/test/test_billing.py::TestUnlicensedBillingAPI --no-cov
.codex/with-flox hogli test frontend/src/loadPostHogJS.test.ts --runInBand
.codex/with-flox hogli test frontend/src/scenes/onboarding/legacy/sdks/hooks/useAdblockDetection.test.ts --runInBand
```

Если обновление затрагивает соответствующие области, дополнительно выполните:

```sh
.codex/with-flox hogli test posthog/test/test_middleware.py::TestCSPMiddleware posthog/test/test_get_context_for_template.py ee/tasks/test/test_send_license_usage.py --no-cov
.codex/with-flox hogli test ee/api/test/test_organization.py::TestOrganizationEnterpriseAPI::test_all_known_features_are_available_without_license posthog/api/test/test_organization.py --no-cov
.codex/with-flox hogli test ee/billing/test/test_quota_limiting.py::TestQuotaLimiting::test_local_entitlements_do_not_mutate_quota_state --no-cov
.codex/with-flox hogli test nodejs/src/common/services/quota-limiting.service.test.ts --runInBand
.codex/with-flox hogli test rust/common/limiters/src/redis.rs
.codex/with-flox hogli test bin/hobby-installer/core
bash -n bin/deploy-hobby
```

Пути приведены для текущей версии. Если upstream перенёс тест или код, найдите
новое расположение и обновите checklist; не пропускайте проверку молча. При новых
путях отправки добавьте случай в ближайший существующий тест согласно `writing-tests`.

Проверка запрета сети должна работать при `TEST=False`, `OPT_OUT_CAPTURE=False`
и в релевантных DEBUG/Cloud/self-hosted режимах. Иначе штатное тестовое отключение
SDK может скрыть регрессию. Перехватывайте реальную сетевую границу, например
`requests.sessions.Session.request`, и проверяйте отсутствие вызовов. Для httpx,
браузера, Go и Rust нужны их собственные границы. Исключение от mock недостаточно:
SDK может его проглотить и поставить отправку в очередь на повтор.

Дополнительно проверьте, что профиль features записывается при создании организации,
API и прямое чтение БД согласованы, синхронизация не перезаписывает одинаковый профиль,
а отключённые billing-квоты не создают блокирующих записей. Не удаляйте проверки
membership, permissions, изоляции команд или защиту от технической перегрузки.

## 4. Проверить поведение браузера и сервера

На изолированной тестовой установке с синтетическими данными заблокируйте исходящие
соединения к Cloud и записывайте попытки соединения. Делайте это в тестовом окружении,
не меняя системный firewall рабочей машины или production. Блокировка нужна до старта
процессов и открытия браузера; даже регрессия не должна отправить реальные данные.

- Проверьте старт web и используемых workers, страницу входа, вход пользователя,
  открытие аналитики, организации и billing, а также затронутые фоновые задачи.
- В браузере записывайте запросы, включая workers, beacon и загрузку скриптов;
  смотрите также CSP и `Reporting-Endpoints` в HTTP-ответах. Чистый DevTools Network
  не подтверждает отсутствие серверных отправок.
- На сервере учитывайте DNS, HTTP(S), фоновые SDK-очереди и повторные попытки.
  Время наблюдения должно покрывать flush/retry-интервалы затронутого клиента.
- Проверьте успешное принятие тестового события локальным PostHog и отсутствие
  ошибок/бесконечных повторов у локальных billing-операций.

Критерий успеха: нет попыток собственной telemetry/billing-отправки в Cloud,
включая заблокированные попытки, а локальные сценарии завершаются. Работа интерфейса
при заблокированной сети сама по себе не доказывает, что приложение не пытается отправлять.

## 5. Зафиксировать результат проверки

Исправляйте регрессии в общих точках создания клиента, транспорта или локального
ответа. Предпочитайте малый diff с сохранением upstream-сигнатур и логики. Не
переписывайте подсистему и не удаляйте upstream-тесты только ради бесконфликтного merge.

В отчёте об обновлении укажите прежний и новый SHA, найденные новые пути отправки,
выполненные тесты, результат наблюдения за сетью и необходимые исправления.
Если отсутствует окружение, зависимость или возможность runtime-проверки, назовите
конкретную причину и пометьте соответствующую проверку как **не выполненную**.
Не объявляйте автономный режим проверенным только по Ruff, compileall или успешному merge.

Обновите этот checklist при изменении точек перехвата и контрактов. Не отправляйте
`hogli devex:feedback` и другие диагностические отчёты в PostHog Cloud. Создание
коммита или push выполняйте только в рамках разрешения пользователя.
