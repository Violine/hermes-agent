# Quota sources and trust levels

Исследование выполнено 26 октября 2026 года на локальном исходнике Hermes
0.20.6 и read-only probe Kimi Coding из контейнера `hermes-vpn-hermes`. Probe
не выполнял login, refresh или изменение состояния подписки; выводились только
HTTP status, названия полей, квотные числа и reset timestamps. API-токены и
сырой JSON не сохранялись и не печатались.

## Что уже умеет Hermes

В Hermes 0.20.6 точка входа `agent.account_usage.fetch_account_usage()` в
`agent/account_usage.py` поддерживает `openai-codex`, `anthropic` и
`openrouter`. Для этой команды важны два источника:

| Подписка | Read-only источник | Поля окна | Статус источника |
| --- | --- | --- | --- |
| Codex по ChatGPT OAuth | Hermes отправляет `GET` на Codex backend `/usage`: для base URL с `/backend-api` это `/wham/usage`, иначе `/api/codex/usage`. По умолчанию base URL — `https://chatgpt.com/backend-api/codex`. Запрос использует Bearer access token и, если доступен, `ChatGPT-Account-Id`. | `rate_limit.primary_window` и `secondary_window`: `used_percent`, `reset_at`, опционально `limit_window_seconds`. | Подтверждён текущей реализацией и mock-тестом Hermes. Это endpoint Codex CLI backend, не публичный OpenAI API contract; upstream может изменить его. Hermes умеет получить token через свой штатный credential resolver. |
| Claude Code / Claude subscription по OAuth | `GET https://api.anthropic.com/api/oauth/usage`; Hermes отправляет OAuth Bearer token, `anthropic-beta: oauth-2025-04-20` и Claude Code User-Agent. | `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`: `utilization` и `resets_at`; `utilization` может быть долей 0..1 либо процентом 0..100. | Endpoint и парсер есть в Hermes 0.20.6. Это OAuth usage endpoint, не описанный в публичном Anthropic Messages API; поддерживается только OAuth-токеном, Anthropic API key не даст subscription quota. Контракт может меняться. |
| Kimi Coding subscription | Сам Hermes `fetch_account_usage()` Kimi не поддерживает. Read-only endpoint Kimi CLI: `GET https://api.kimi.com/coding/v1/usages`, Bearer Kimi Code key. | Текущий ответ в нашем probe: корневой `usage` с `limit`, `used`, `remaining`, `resetTime`; `limits[]` с `detail.{limit,used,remaining,resetTime}` и `window.{timeUnit}`. Ответ также содержал пустой `usages[]`. | Endpoint сработал с существующим ключом в защищённом контейнере (HTTP 200). Контракт не опубликован как официальный API. Старый Python `MoonshotAI/kimi-cli` был архивирован 23 сентября 2026 года и заменён новым Kimi Code CLI, поэтому считать endpoint стабильным публичным API нельзя. |

Выполненный Kimi probe увидел `usage.used=58`, `limit=100`, `remaining=42`,
reset `2026-10-31T11:18:20.104999Z`; у `limits[0].detail` было `used=44`,
`limit=100`, `remaining=56`, reset `2026-10-27T00:18:20.104999Z`. `window`
содержал `timeUnit=TIME_UNIT_MINUTE`, но длительности в ответе не было. Успешный
HTTP status доказывает доступность этого ключа и endpoint в момент запроса, но
не официальную поддержку или долгосрочную совместимость.

Это общая квота подписки: её могут одновременно расходовать другие клиенты.
Изменение процентов между снимками нельзя приписывать конкретной задаче команды.

### Kimi API balance — отдельный продукт

Kimi/Moonshot PAYG API имеет официальный balance endpoint:
`GET https://api.moonshot.ai/v1/users/me/balance` (China endpoint:
`https://api.moonshot.cn/v1/users/me/balance`) с API-key Bearer auth. Он
возвращает денежный баланс, а не окна квот и не reset. Не смешивать его с
Kimi Coding subscription endpoint: наличие balance не должно становиться
`used_percent` или сигналом доступности подписочного пула.

## Источники и проверяемые места

- Hermes source: [`agent/account_usage.py`](https://github.com/NousResearch/hermes-agent/blob/main/agent/account_usage.py) — `AccountUsageSnapshot`, Codex URL resolver, Codex/Anthropic fetchers и provider dispatch. Описание отражает прежнее состояние источников и требует сверки с используемой версией Hermes и провайдеров.
- Hermes Codex fixture test: [`tests/test_account_usage.py`](https://github.com/NousResearch/hermes-agent/blob/main/tests/test_account_usage.py) — окно `primary_window` / `secondary_window`, percent и reset parsing.
- [Kimi Code CLI FAQ](https://moonshotai.github.io/kimi-cli/en/faq.html) — подтверждает наличие пользовательской команды `/usage` для просмотра quota и membership.
- [MoonshotAI/kimi-cli](https://github.com/MoonshotAI/kimi-cli) — GitHub указывает, что Python CLI архивирован и заменён Kimi Code CLI.
- [Kimi Code API balance docs](https://platform.kimi.ai/docs/api/balance) — публичный PAYG balance endpoint; это баланс, не подписочные лимиты.
- [Community endpoint research for Kimi](https://github.com/wez/quotas/blob/master/docs-usage/kimi.md) — описывает `/coding/v1/usages` по старому CLI source. Это вторичный источник; используем только как discovery, сверяем с текущим read-only ответом и не приписываем ему официальный статус.
- [Anthropic API overview](https://platform.claude.com/docs/en/api/overview) описывает публичный model API и не документирует OAuth subscription usage endpoint, используемый Hermes.

## Рекомендации для scheduler snapshots

Один snapshot должен представлять фактическое наблюдение, а не прогноз:

```json
{
  "source": "kimi-coding-usages",
  "observed_at": 1793048061.5,
  "windows": [
    {"name": "weekly", "used_percent": 58.0, "reset_at": 1793445500.0},
    {"name": "limit_0", "used_percent": 44.0, "reset_at": 1793060300.0}
  ],
  "error": null
}
```

Для Kimi процент вычисляется из `used / limit * 100`; если `used` отсутствует,
допускается `(limit - remaining) / limit * 100`. Не делить напрямую на 100 и не
предполагать, что весь usage всегда является процентом. Нужны положительный
`limit` и согласованные конечные числа; некорректное/неполное окно не должно
превращаться в нулевой расход. `reset_at` берётся из `resetTime`; не выводить
его из `timeUnit`, если ответа сервера не хватает для timestamp. Включать все
известные окна: планирование должно считать минимальный остаток среди окон.

Codex и Claude следует опрашивать через уже существующий Hermes helper, чтобы
использовать штатную OAuth credential resolution и парсеры. Kimi требует
отдельного read-only GET и Kimi Code ключа (`sk-kimi-*`) из Hermes `.env`; не
отправлять Moonshot PAYG API keys на этот endpoint. Запросы Kimi используют
8-секундный timeout без повторов. Controller ждёт каждый probe не более
9 секунд. Hermes helper для Codex/Claude создаёт HTTP client с 15-секундным
timeout и не имеет параметра timeout; если 9-секундное ожидание controller
закончится, его daemon worker может продолжить работу, пока helper не вернёт
ответ. Значит ожидание controller ограничено, но underlying Hermes network
call не отменяется принудительно на 9-й секунде. Не заявлять о жёсткой отмене
запроса, пока Hermes не добавит настраиваемый timeout или отдельный адаптер не
использует resolver и ограниченный GET. Не логировать URL-заголовки или raw
exception body. Хранить только normalised windows, `source`, UTC `observed_at`
и обезличенную ошибку.

Если endpoint недоступен, авторизация отсутствует/не подходит или окно нельзя
нормализовать, записывать `error` с пустым списком окон. Для scheduler это
`unknown`, не `0% used`. Отмечать snapshot stale после короткого настроенного
TTL и не выбирать pool автоматически из неофициального/устаревшего источника.
Usage данные не дают точного прогнозирования стоимости каждой задачи; резерв
по остаткам и concurrency нужны отдельно. Текущие Hermes Codex/Claude helpers и
Kimi root `.env` читают по одному глобальному auth source на provider. Поэтому
несколько pool IDs, настроенных на один и тот же canonical provider, нельзя
считать отдельными подписками или балансировать как разные аккаунты: все такие
pools должны остаться `unknown`, пока не появится явная поддержка per-pool
credential scope и независимое подтверждение account identity.

## Ограничения и безопасность

- Токены не должны попадать в snapshot-файл, stdout, exception text или task
  card. Выводить можно только провайдера, статус, агрегированные проценты,
  timestamps, длительность запроса и безопасный тип ошибки/HTTP status.
- `account_usage.fetch_account_usage()` для Codex может запустить штатный
  Hermes credential refresh, если access token истекает. Это существующий
  read path Hermes; не вызывать interactive login или reset/redeem endpoint.
- Kimi GET endpoint верифицирован одной текущей read-only выборкой, но remains
  undocumented. При неудаче fail-closed в `unknown`, без попыток угадать путь,
  повторять разные endpoints или читать/показывать ключ.
- Изоляция процессов под общим Unix UID не делает credential store границей
  безопасности. Quota collector также должен запускаться под тем же Hermes
  VPN/network policy, что и агенты, и хранить snapshots с ограниченными
  правами.
