# Моки внешних систем

| Мок | Заменяет | Контракт | Порт (compose) |
|-----|----------|----------|----------------|
| `speechkit/` | Yandex SpeechKit v1 | `POST /speech/v1/stt:recognize` (тело — аудио, заголовок `Authorization: Api-Key …`) → `{"result": "текст"}` | 8091 |
| `gigachat/` | GigaChat API | `POST /api/v2/oauth` (Basic) → `access_token`; `POST /api/v1/chat/completions` (Bearer) → `choices[0].message.content` | 8092 |

Сервисы используют **настоящие адаптеры** (`transcription_service/providers.py`, `analysis_service/providers.py`).
Мок отличается от облака только адресом, поэтому переход на реальные API сводится к переменным окружения
`SPEECHKIT_URL`, `SPEECHKIT_API_KEY`, `YC_FOLDER_ID`, `GIGACHAT_URL`, `GIGACHAT_AUTH_URL`, `GIGACHAT_AUTH_KEY`.

Управление сбоями для демонстрации:

```bash
curl -X POST localhost:8091/__admin/mode -H 'Content-Type: application/json' -d '{"mode":"fail"}'      # SpeechKit 503
curl -X POST localhost:8091/__admin/mode -H 'Content-Type: application/json' -d '{"mode":"bad_audio"}' # SpeechKit 400
curl -X POST localhost:8092/__admin/mode -H 'Content-Type: application/json' -d '{"mode":"fail"}'      # GigaChat 503
curl -X POST localhost:8092/__admin/mode -H 'Content-Type: application/json' -d '{"mode":"bad_json"}'  # ответ не JSON
curl -X POST localhost:8091/__admin/mode -H 'Content-Type: application/json' -d '{"mode":"ok"}'
curl localhost:8091/__admin/state                                                                      # счётчик вызовов
```

Как мок SpeechKit «распознаёт» речь: демо-файлы `demo/audio/*.ogg` содержат маркер `TSDEMO` и текст сценария с
метками дикторов (`S1: …`, `S2: …`), который мок и возвращает. Как мок GigaChat «анализирует»: простые правила по
ключевым словам («закончился», «нет в меню», «сколько можно», «спасибо большое» и т. п.).
