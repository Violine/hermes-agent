# Экспорт доработок в личный форк

Дата проверки исходников: **19 ноября 2026 года**.

Форк: <https://github.com/Violine/hermes-agent>.
Ветка: `codex/engineering-team-flow`.
База сравнения: `codex/hermes-deployed-base`,
`5cc1369fa298021f8c740de154ff8c37c30bdcc8` (Hermes 0.20.6).

## Откуда взят код

Встроенные файлы Hermes сверены с рабочей копией сервера. В экспорт вошёл
каждый изменённый tracked-файл и четыре новых файла словарей React.
Содержимое `hermes-team` сверено с установленным runtime по SHA-256.
Взята локальная версия пакета: она дополнительно содержит тесты и CSS,
отсутствовавшие в runtime. Внешние host test helpers также сверены; они входят
в `scripts/`, а `backend_test_jobs.py` хранится единственным каноническим
исходником в `team_engine/`.

`../SOURCE-MANIFEST.json` содержит полный список включённых файлов, их
контрольные суммы, происхождение и намеренные отличия от исходного снимка.
Документация обезличена; технический код и публичные контракты сохранены.
Рабочие копии сервера и первоначального локального пакета не изменены.

## Что проверено в этой ветке

| Проверка | Результат |
| --- | --- |
| Python-тесты Hermes Team через штатный runner | 548 passed, 40 файлов |
| Handwritten JS команды и Kanban | 4 набора прошли |
| `tests/agent/test_account_usage.py` | 9 passed, в том числе 5 значений процентов Anthropic |
| Native Kanban DB/decomposition | 34 passed, 1 Windows-only skip на macOS |
| Auth locking/profile fallback/provider boundary | 14 passed, 1 Windows-only skip на macOS |
| Основной web UI, Vitest | 279 passed, 36 файлов |
| Основной web UI, TypeScript + Vite build | Успешно |
| Синтаксис затронутого Python core, shell wrappers и JS | Успешно |
| Полнота переноса исходников | 51 изменённый файл Hermes совпадает с серверным снимком; 142 канонических файла командного пакета учтены |
| Комплект установки | Dry run без записи, создание нового каталога и отказ перезаписывать существующий проверены |

Это проверки исходников и поведения в изолированных тестах. Новый live запуск
конвейера с моделями, установка на другой сервер и проверка всех внешних
провайдеров в этот экспорт не входят. Исторические отчёты не объявляются
повторно выполненными проверками.

Для Python используется `scripts/run_tests.sh` из корня форка. Например,
после подготовки совместимого venv с pytest:

```sh
HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 \
  scripts/run_tests.sh contrib/hermes-team/tests -q
scripts/run_tests.sh tests/agent/test_account_usage.py -q
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_decompose.py -q
```

Проверки JS из корня форка:

```sh
node contrib/hermes-team/tests/test_dashboard_attachments_ui.cjs
node contrib/hermes-team/tests/test_dashboard_workflow_ui.cjs
node contrib/hermes-team/tests/test_kanban_operator_status.cjs
node contrib/hermes-team/tests/test_kanban_usability_ui.cjs
npm ci --workspace=web --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org --replace-registry-host=always
npm run build --workspace=web
npm run test --workspace=web
```

Если локальный npm перенаправляет запросы на корпоративное зеркало,
для воспроизведения этой проверки используются явно указанные публичный
registry и `replace-registry-host`. Конфигурация пользователя не изменяется.

## Подготовка комплекта для установки

Из корня форка можно получить план размещения без записи:

```sh
python3 contrib/hermes-team/scripts/stage_distribution.py /tmp/hermes-team-staging-new
```

С `--write` создаётся **новый** каталог. Существующий путь отклоняется.
Скрипт читает только исходники пакета, не подключается к серверу и не запускает
службы или bootstrap. Он подготавливает следующую структуру:

| Каталог комплекта | Назначение при отдельной установке |
| --- | --- |
| `runtime/` | `<Hermes root>/team/runtime` |
| `plugin/engineering-team/` | `<Hermes root>/plugins/engineering-team` |
| `themes/` | `<Hermes root>/dashboard-themes` |
| `host-tools/` | Каталог доверенных host helpers из конфигурации установки |
| `systemd/` | Шаблоны user units после проверки путей и ExecStart |

Нативные core/React/встроенные plugin-изменения уже находятся в форке.
`npm run build --workspace=web` создаёт `hermes_cli/web_dist/`, который
не включён в Git. Архивные `dashboard/patches/*.patch` нужны для анализа
переноса на исходную версию; повторно применять их к этому форку нельзя.

На другой машине сначала адаптируйте Linux wrappers, имя контейнера,
fixed paths, allowlist проекта доверенных проверок и доступность toolchains.
В частности, backend test bridge специально привязан к проверенному проекту,
rootless Docker и файловой песочнице; он не разрешает произвольную команду
от агента. Это ограничение сохраняется при переносе.

Настоящая установка — отдельная операция: резервная копия затрагиваемых
исходников, проверка отсутствия активных workers, доставка runtime и helpers,
настройка профилей/провайдеров владельцем и целевой smoke test. Нативные
worker hooks требуют установленного командного runtime. Не запускайте их
до его размещения. Конфигурация, credentials и данные задач в комплект не
входят и не должны перезаписываться при обновлении кода.

## Что намеренно не публикуется

Исключены auth/config/env текущего пользователя, Kanban DB, runtime state,
записи диалогов и модели, журналы, operation archives, резервные копии,
CRM snapshots, исходные бизнес-документы, screenshots с данными и внутренние
корпоративные источники. Разовые `recover`/`deploy` скрипты, которые меняли
конкретные карточки, заменены конечными исправлениями в исходниках и тестах.

Отдельные встроенные патчеры и host adapters сохраняются как source для
совместимости существующей установки; их наличие не означает автоматического
разрешения менять работающую среду. Ветки форка сами по себе ничего не
развёртывают на домашнем сервере.
