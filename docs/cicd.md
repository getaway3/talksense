# CI/CD TalkSense (ЛР5)

## 1. Выбор платформы — GitHub Actions

- Репозиторий проекта уже на GitHub: пайплайн живёт рядом с кодом (`.github/workflows/`), отдельный сервер (Jenkins, TeamCity) поднимать не нужно.
- **GitHub Container Registry** встроен: образы публикуются с временным `GITHUB_TOKEN`, который GitHub выдаёт на время прогона. Хранить пароль реестра в репозитории не нужно.
- Матрицы и переиспользуемые workflow (`workflow_call`) позволяют описать пайплайн сервиса один раз и запустить его для всех 8 образов.
- Встроенный кэш (`actions/setup-python` для pip, `type=gha` для слоёв Docker) и бесплатные раннеры для публичных репозиториев.
- На раннере `ubuntu-latest` есть Docker и Docker Compose, поэтому staging-деплой делается прямо на раннере.

## 2. Устройство пайплайна

```
                       ┌───────────────────────────────────────────────┐
push / PR в main ──►   │ api-contracts: Redocly lint + AsyncAPI validate│
                       └──────────────┬────────────────────────────────┘
                 ┌────────────────────┴─────────────────────┐
                 ▼                                          ▼
  services (матрица × 8, service.yml)               integration
  ┌──────────────────────────────────┐              pytest services/common/tests tests
  │ test:    ruff → compileall → pytest│            (сага через 6 сервисов)
  │ package: docker build ─► Trivy     │
  │          push в GHCR (только main) │
  └──────────────────┬───────────────┘
                     └──────────────┬─────────────────────┘
                                    ▼  (только push в main)
                    deploy-staging: docker compose pull/up --wait
                                    (образы из GHCR, docker-compose.staging.yml)
                                    smoke-тест demo/run_demo.py через шлюз
                                    docker compose down
```

| Файл | Назначение |
|------|------------|
| `.github/workflows/ci.yml` | главный пайплайн: триггеры, матрица сервисов, интеграционные тесты, деплой |
| `.github/workflows/service.yml` | переиспользуемый пайплайн одного сервиса (`workflow_call`): build → test → package → publish |
| `docker-compose.staging.yml` | override: вместо `build` берутся опубликованные образы `ghcr.io/<owner>/talksense-<name>:<sha>` |
| `scripts/ci-local.sh` | те же проверки локально (без Docker) |

### Этапы

| Этап | Что делает | Когда |
|------|-----------|-------|
| **lint контрактов** | `redocly lint` для 6 OpenAPI, `asyncapi validate` | всегда |
| **build** | установка зависимостей (кэш pip), `ruff check`, `python -m compileall` | всегда, для каждого из 6 сервисов |
| **test** | unit- и API-тесты сервиса (`pytest`), JUnit-отчёт сохраняется как артефакт | всегда |
| **integration** | 22 теста: общая библиотека + сквозная сага через все 6 сервисов | всегда |
| **package** | `docker build` (кэш слоёв в GitHub Actions cache), 6 сервисов + 2 мока; сканирование образа Trivy (CRITICAL/HIGH, информационно) | всегда |
| **publish (CD)** | push образов в GHCR с тегами `<sha7>` и `latest` | push в `main` |
| **deploy staging (CD)** | `docker compose up --wait` из опубликованных образов на раннере, smoke-тест сценариев через шлюз, логи при ошибке, `down -v` | push в `main` |

Требования методички: CI для **8 образов** (нужно ≥ 3); сборка, unit-тесты и Docker-образ есть у каждого сервиса;
CD — публикация в реестр **и** деплой в staging; триггеры — `push` и `pull_request` в `main`; секреты не хранятся в коде.

### Секреты

| Секрет | Откуда | Зачем |
|--------|--------|-------|
| `GITHUB_TOKEN` | выдаётся GitHub автоматически на каждый прогон, права ограничены `permissions:` (`packages: write` только у job сборки) | вход в GHCR |
| `STAGING_SERVICE_TOKEN` (необязательный) | Settings → Secrets and variables → Actions | сервисный токен staging; если не задан, используется тестовое значение |
| `SPEECHKIT_API_KEY`, `GIGACHAT_AUTH_KEY` | на будущее, для стенда с реальными API | в ЛР работают моки, ключи не нужны |

В коде и в `docker-compose.yml` секретов нет: значения берутся из переменных окружения (`${SERVICE_TOKEN:-dev-service-token}` — dev-значение по умолчанию только для локального запуска).

### Ускорение

- `actions/setup-python` с `cache: pip`: зависимости не скачиваются заново, если не менялся `services/requirements-dev.txt`;
- `cache-from/cache-to: type=gha`: слои Docker переиспользуются, при изменении только кода пересобирается последний слой (зависимости в Dockerfile ставятся отдельным слоем до `COPY` кода);
- матрица `fail-fast: false`: сервисы собираются параллельно, падение одного не отменяет остальные;
- `concurrency`: новый push в ту же ветку отменяет устаревший прогон.

## 3. Неуспешный прогон

Если сломать бизнес-логику (например, убрать в `analysis_service/logic.py` правило «позиция есть в меню → `OUT_OF_STOCK`»),
падает unit-тест сервиса:

```
>       assert inc["type"] == "OUT_OF_STOCK"
E       AssertionError: assert 'MENU_REQUEST' == 'OUT_OF_STOCK'
FAILED tests/test_logic.py::test_menu_request_for_existing_item_becomes_out_of_stock
1 failed, 18 passed in 0.11s
```

Как пайплайн это обрабатывает:
1. job `analysis-service / build & test` завершается с ошибкой, JUnit-отчёт всё равно загружается (`if: always()`);
2. job `analysis-service / docker image` не запускается (`needs: test`): **образ со сломанным кодом не собирается и не публикуется**;
3. остальные сервисы проходят (`fail-fast: false`), видно, что сломан только один;
4. `deploy-staging` пропускается (`needs: [services, integration]`), staging остаётся на последней рабочей версии;
5. в Pull Request проверка красная. Если включить branch protection (Settings → Branches → Require status checks), смержить такой PR нельзя.

## 4. Как проверить

**На GitHub:**
1. Запушить репозиторий в `main` → вкладка **Actions** → workflow `ci`.
2. Для демонстрации падения: `git checkout -b broken`, внести ошибку, `git push -u origin broken`, открыть Pull Request в `main`.
3. Опубликованные образы: профиль/организация → **Packages** (`talksense-*`).
4. Для первого push в GHCR: Settings → Actions → General → Workflow permissions → **Read and write permissions**.

**Локально** (те же проверки без Docker):

```bash
pip install -r services/requirements-dev.txt
bash scripts/ci-local.sh
```

Локальный запуск workflow целиком возможен через [`act`](https://github.com/nektos/act): `act pull_request`.

**Скриншоты для отчёта** (сделать после первого прогона на GitHub):
- [ ] граф успешного прогона (`ci` → summary с матрицей сервисов);
- [ ] лог шага `Unit tests` любого сервиса;
- [ ] шаг `Build & push` и пакеты в GHCR;
- [ ] job `deploy to staging + smoke test`: вывод `run_demo.py`;
- [ ] красный прогон из PR с упавшим тестом и пропущенными `docker image` / `deploy`.

**Готовые скриншоты** (`docs/screenshots/`):

| Файл | Что показывает |
|------|----------------|
| `cicd-green-run.jpg` | успешный прогон `ci` на `main`: lint → матрица сервисов → integration → deploy to staging |
| `cicd-red-run.jpg` | красный прогон из Pull Request: сервис `analysis-service` упал, `deploy` пропущен |
| `cicd-red-pr.png` | Pull Request #1 с красным крестиком у коммита (закрыт без слияния, ветка `demo/broken-test`) |
| `github-repo.png` | главная страница репозитория |

Ссылки: [успешный прогон](https://github.com/getaway3/talksense/actions/runs/44832127225),
[красный прогон](https://github.com/getaway3/talksense/actions/runs/44833304755),
[Pull Request](https://github.com/getaway3/talksense/pull/1).
