# Проверенные сценарии (ЛР4)

Все сценарии запускаются одной командой `python demo/run_demo.py` после `docker compose up -d --build`
и выполняются **через API Gateway** (`http://localhost:8080`). Полный вывод последнего прогона —
[docs/demo/run_demo_output.txt](demo/run_demo_output.txt), выдержка из логов сервисов —
[docs/demo/logs_excerpt.txt](demo/logs_excerpt.txt). Те же запросы есть в Postman-коллекции
[postman/TalkSense.postman_collection.json](../postman/TalkSense.postman_collection.json)
(прогон через `newman`: 22 запроса, 19 проверок, 0 ошибок).

## Что реально, а что мок

| Взаимодействие | Как реализовано |
|----------------|-----------------|
| Клиент/устройство → API Gateway → сервисы | **реально**: Nginx-шлюз, маршрутизация по путям, `/internal/*` закрыт |
| S1 device → venue, S2 recording → device, S3 transcription → recording, S4 analysis → transcription, S5 analysis → venue, S6 report → venue | **реально**: HTTP между контейнерами, сервисный токен, таймаут 2 с, повторы GET, circuit breaker |
| Все 8 типов событий | **реально**: RabbitMQ, topic exchange `talksense.events`, очередь + очередь повторов (TTL) + DLQ у каждого подписчика |
| transcription → Yandex SpeechKit | **мок** `mocks/speechkit` с контрактом SpeechKit v1 (`POST /speech/v1/stt:recognize`, `Api-Key`). Адаптер настоящий: для реального облака достаточно сменить `SPEECHKIT_URL` и ключ |
| analysis → GigaChat | **мок** `mocks/gigachat` с контрактом GigaChat (`/api/v2/oauth`, `/api/v1/chat/completions`). Вместо LLM — правила по ключевым словам, чтобы сценарии были воспроизводимыми |
| MinIO | **заменён** каталогом в томе `recording-data`; presigned URL — ссылка с HMAC-подписью и сроком действия 15 мин |
| PostgreSQL / MongoDB / Redis | **заменены** SQLite (отдельный файл в отдельном томе у каждого сервиса — разрешено методичкой) |
| Keycloak | **не поднимается**: сервисная аутентификация — общий токен `SERVICE_TOKEN`, проверка JWT пользователей на шлюзе не реализована |

## Сценарий 1. Успешный путь (happy path)

Устройство отправляет 5 фрагментов разговоров (`demo/scripts/*.txt`): «нет лавандового рафа», «добавьте матчу на
кокосовом», конфликт из-за ожидания, личный разговор бариста, благодарность.

Ожидаемо и получено:

| Фрагмент | Путь по саге | Итог |
|----------|--------------|------|
| 01 нет в наличии | UPLOADED → TRANSCRIBED → ANALYZED | `OUT_OF_STOCK`, гость сказал «лавандовый раф» → сопоставлено с позицией меню «Раф лавандовый» |
| 02 просьба добавить | UPLOADED → TRANSCRIBED → ANALYZED | `MENU_REQUEST` «матча на кокосовом» (в меню нет) |
| 03 конфликт | UPLOADED → TRANSCRIBED → ANALYZED | `CONFLICT`, `HIGH`, попал в «Главное за день» |
| 04 личный разговор | UPLOADED → TRANSCRIBED → **DISCARDED** (`PERSONAL_ONLY`) | аудио и расшифровка удалены, в отчёт не попал |
| 05 благодарность | UPLOADED → TRANSCRIBED → ANALYZED | `PRAISE` |

Отчёт `GET /api/v1/reports/daily/{venueId}/{date}`: `recordingsAnalyzed: 4`, по одному инциденту каждого типа,
`outOfStock: [Раф лавандовый ×1]`, `menuRequests: [матча на кокосовом ×1]`. У всех проанализированных записей
`audioPurged: true`: исходное аудио удаляется сразу после анализа.

Путь одной записи прослеживается в логах всех сервисов по `correlationId = recordingId`:

```
transcription-service [cid=8da1acce…] получено recording.uploaded (попытка 1)
transcription-service [cid=8da1acce…] опубликовано transcription.completed
analysis-service      [cid=8da1acce…] получено transcription.completed (попытка 1)
recording-service     [cid=8da1acce…] получено transcription.completed (попытка 1)
recording-service     [cid=8da1acce…] получено analysis.completed (попытка 1)
report-service        [cid=8da1acce…] получено analysis.completed (попытка 1)
```

## Сценарий 2. Сбой распознавания и компенсация

1. `POST http://localhost:8091/__admin/mode {"mode":"fail"}`: мок SpeechKit отвечает 503.
2. Устройство загружает фрагмент → `201`, статус `UPLOADED`.
3. transcription-service получает `recording.uploaded`, SpeechKit отвечает 503 → сообщение уходит в
   `transcription-service.events.retry` и возвращается через 2 с, затем через 5 с (логи ниже).
4. После 3-й неудачной попытки публикуется `transcription.failed {reason: PROVIDER_UNAVAILABLE, attempts: 3}`.
5. recording-service переводит запись в `FAILED_TRANSCRIPTION`. **Компенсация:** аудио не удаляется
   (`audioPurged: false`), чтобы обработку можно было повторить.
6. Мок возвращается в `ok`, `POST /api/v1/recordings/{id}/reprocess` (с `Idempotency-Key`) → `202`, `attempt: 2`.
7. Запись проходит конвейер заново → `ANALYZED`, аудио удалено.

```
00:34:17,768 WARNING transcription-service [cid=ca6d3953…] повтор e08240a9… через 2000 мс: SpeechKit HTTP 503
00:34:19,770 INFO    transcription-service [cid=ca6d3953…] получено recording.uploaded (попытка 2)
00:34:19,782 WARNING transcription-service [cid=ca6d3953…] повтор e08240a9… через 5000 мс: SpeechKit HTTP 503
00:34:24,785 INFO    transcription-service [cid=ca6d3953…] получено recording.uploaded (попытка 3)
00:34:24,797 INFO    transcription-service [cid=ca6d3953…] опубликовано transcription.failed
00:34:24,797 INFO    recording-service     [cid=ca6d3953…] получено transcription.failed (попытка 1)
```

Аналогично проверены интеграционными тестами (`tests/test_saga.py`):
- **сбой GigaChat** → `FAILED_ANALYSIS`, аудио и расшифровка сохранены; при `reprocess` transcription-service
  отдаёт готовую расшифровку и **не вызывает платный SpeechKit повторно**;
- **GigaChat вернул не-JSON** → ответ не проходит валидацию схемы → повторы → `FAILED_ANALYSIS (LLM_BAD_RESPONSE)`;
- **закрытие точки** → `venue.deactivated` → device-service отзывает устройства → загрузка с них даёт `403`;
- **повторная доставка события** (at-least-once) → потребители видят `eventId` в `processed_events`, отчёт не
  задваивается.

## Сценарий 3. Идемпотентность

Повторная отправка фрагмента с тем же `Idempotency-Key` (устройство не получило ответ и повторило) →
`200 OK`, заголовок `Idempotent-Replayed: true`, тот же `id`, второго события `recording.uploaded` нет.
Тот же ключ с другим телом запроса на `POST /api/v1/venues` или `POST /api/v1/devices` → `422`.

## Сценарий 4. Шлюз и единая модель ошибок

- `GET /internal/v1/venues/{id}` через шлюз → `404 application/problem+json`: internal-API снаружи недоступен.
- Загрузка с чужим токеном устройства → `401`:
  ```json
  {"type": "https://talksense.local/problems/unauthorized", "title": "Unauthorized", "status": 401,
   "detail": "Неизвестный токен устройства", "instance": "/device/v1/recordings", "traceId": "3b67169b…"}
  ```
- Невалидный параметр (`venueId=not-uuid`) → `400` с полем `errors: [{"field": "venueId", ...}]`.

## Скриншоты запуска в Docker (Windows, Docker Desktop)

| Файл | Что показывает |
|------|----------------|
| `docs/screenshots/lab4-docker-compose-ps.png` | `docker compose ps`: все сервисы запущены, у сервисов статус `healthy` |
| `docs/screenshots/lab4-demo-scenario1.png` | сценарий 1: успешный сквозной путь, отчёт за день |
| `docs/screenshots/lab4-demo-scenarios2-4.png` | сбой SpeechKit с компенсацией, идемпотентность, ошибки шлюза |
| `docs/screenshots/lab4-rabbitmq-queues.jpg` | консоль RabbitMQ: очереди `*.events`, `*.retry`, `*.dlq` |

Скриншоты вывода команд сделаны из текста реального запуска `run_demo.py` и `docker compose ps`.
