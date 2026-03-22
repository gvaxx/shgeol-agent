# microagent

Минималистичный CLI-агент для работы с кодом. Один файл, ноль зависимостей.

```
python agent.py "добавь обработку ошибок в main.py"
```

---

## Особенности

- **Один файл** — `agent.py`, ~1100 строк, копируй куда угодно
- **Ноль зависимостей** — только Python stdlib (3.10+)
- **OpenAI-compatible API** — работает с OpenAI, OpenRouter, Ollama, vLLM, LM Studio, llama.cpp
- **6 инструментов** — read, write, patch, ls, grep, shell
- **Контекст репозитория** — сканирует код через `ast`, генерирует описание, инжектирует в каждую сессию
- **История сессий** — автосохранение, возобновление, суммаризация при переполнении
- **Отмена генерации** — Ctrl+C закрывает HTTP-соединение, vLLM/Ollama останавливают инференс

---

## Установка

```bash
git clone https://github.com/gvaxx/shgeol-agent
cd shgeol-agent
# всё, больше ничего не нужно
```

Python 3.10+, зависимости отсутствуют.

---

## Настройка

Переменные окружения или `.env` файл в текущей директории:

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # по умолчанию
OPENAI_MODEL=gpt-4o                          # по умолчанию
WORKDIR=.                                    # по умолчанию — текущая директория
MAX_CONTEXT_CHARS=1000000                    # ~256k токенов, по умолчанию
MAX_SESSIONS_KEEP=50                         # сколько сессий хранить, по умолчанию
```

### Примеры конфигурации

```bash
# OpenAI
OPENAI_API_KEY=sk-... python agent.py

# OpenRouter
OPENAI_API_KEY=sk-or-... OPENAI_BASE_URL=https://openrouter.ai/api/v1 OPENAI_MODEL=qwen/qwen3-30b-a3b python agent.py

# Ollama (локально)
OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_MODEL=qwen2.5-coder:7b OPENAI_API_KEY=ollama python agent.py

# vLLM
OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_MODEL=Qwen/Qwen2.5-Coder-7B-Instruct python agent.py
```

---

## Использование

```bash
# Интерактивный режим (REPL)
python agent.py

# Одноразовый запрос
python agent.py "добавь типы во все функции в utils.py"

# Указать рабочую директорию
WORKDIR=/path/to/project python agent.py

# Переопределить модель или API endpoint прямо в команде
python agent.py --model qwen/qwen3-30b-a3b "исправь баг в parser.py"
python agent.py --url http://localhost:11434/v1 --model qwen2.5-coder:7b

# Сгенерировать .agent_context.md и выйти (удобно для CI или первой настройки)
python agent.py --init

# Возобновить последнюю сессию
python agent.py --resume

# Возобновить конкретную сессию по номеру
python agent.py --resume 2
```

---

## Инструменты агента

| Инструмент | Описание |
|---|---|
| `read_file` | Читает файл (до 50к символов) |
| `write_file` | Создаёт или перезаписывает файл (лимит 2 МБ) |
| `patch_file` | Заменяет ровно одно вхождение строки (ошибка если 0 или >1) |
| `ls` | Список файлов в директории |
| `grep` | Рекурсивный поиск по содержимому |
| `shell` | Запуск команд из whitelist: `find wc head tail sort uniq diff echo pwd date` |

Все пути резолвятся относительно `WORKDIR`, выход за его пределы заблокирован.
`shell` использует `shlex.split` + `shell=False` — инжекция через `$()`, `&&`, `|` невозможна.
Абсолютные пути в аргументах `shell` (например `/etc/passwd`) блокируются если они вне `WORKDIR`.

Каждый вызов инструмента показывается в терминале с кратким результатом:

```
⚡ read_file db.py  → 18 lines, 430 chars
⚡ patch_file db.py  → OK: patched db.py
⚡ grep connect  → 5 lines, 210 chars
```

---

## Контекст репозитория

Агент умеет сканировать проект и сохранять его описание в `.agent_context.md`. Этот файл автоматически инжектируется в системный промпт при каждом запуске — агент знает архитектуру проекта без необходимости читать все файлы заново.

```
> /init
[scanning repo…]
[generating context with AI…]
[written .agent_context.md — 1519 chars]
```

Сканирование использует Python `ast`: извлекает классы, методы, сигнатуры функций и docstring. Никаких внешних зависимостей вроде tree-sitter.

Для неинтерактивной инициализации (например, в скриптах или CI):

```bash
python agent.py --init
```

### Команды контекста

| Команда | Что делает |
|---|---|
| `/init` | Сканирует репо, просит модель написать описание и архитектурные заметки |
| `/reinit` | Пересканирует и совмещает новую карту с существующими заметками |
| `/update` | Просит модель обновить контекст на основе текущей сессии |

---

## Управление сессиями

Контекст разговора сохраняется между сообщениями и автоматически записывается в `.sessions/` после каждого хода.

```
> /sessions
  [0] 2026-03-22T21:25   4 msgs  добавь параметр timeout в db.connect()
  [1] 2026-03-22T20:08   6 msgs  исправь баг в parser.py

> /resume 1
[resumed session_20260322_200806.json — 6 messages]
```

Когда контекст превышает `MAX_CONTEXT_CHARS` (по умолчанию ~256к токенов), агент предлагает суммаризацию:

```
[context is 1,041,230 chars (~260,307 tokens). Summarize and start a fresh session? (y/N)]
```

При согласии модель генерирует компактное резюме (что было сделано, какие файлы затронуты), старая сессия архивируется, новая начинается с резюме вместо полной истории.

Старые файлы сессий удаляются автоматически, когда их количество превышает `MAX_SESSIONS_KEEP` (по умолчанию 50).

### Все команды

| Команда | Описание |
|---|---|
| `/init` | Сгенерировать `.agent_context.md` |
| `/reinit` | Обновить `.agent_context.md` пересканированием |
| `/update` | Обновить `.agent_context.md` по итогам сессии |
| `/sessions` | Список сохранённых сессий с превью первого сообщения |
| `/resume [N]` | Возобновить сессию N (по умолчанию последнюю) |
| `/save` | Принудительно сохранить сессию |
| `/summarize` | Сжать контекст прямо сейчас |
| `/clear` | Очистить контекст (начать заново) |
| `/help` | Показать справку |
| `exit` / `quit` | Сохранить и выйти |

Промпт показывает количество сообщений когда их больше 10: `[24]>`

---

## Структура файлов

После первого использования в рабочей директории появятся:

```
your-project/
├── .agent_context.md   ← описание архитектуры (генерируется /init)
├── .worklog.md         ← лог всех вызовов инструментов
└── .sessions/
    ├── session_20260322_212544.json
    └── session_20260322_200806.json
```

---

## Безопасность

- Все файловые операции ограничены `WORKDIR` (защита от `../` traversal)
- `shell` работает через `shlex.split` + `shell=False` — bash-метасимволы (`$()`, `&&`, `|`, `;`) не интерпретируются
- Абсолютные пути в аргументах `shell` проверяются на принадлежность `WORKDIR`
- `rm`, `cp`, `mv`, `mkdir`, `cat` убраны из whitelist shell — файлы можно трогать только через `read_file`/`write_file`
- `grep` использует `--` перед паттерном — строка вроде `-r` не воспринимается как флаг
- `write_file` ограничен 2 МБ — защита от забивки диска
- Ctrl+C во время генерации закрывает HTTP-соединение — сервер (vLLM/Ollama) останавливает инференс

---

## Пример сессии

```
$ WORKDIR=/my/project python agent.py --model qwen/qwen3-30b-a3b
microagent  |  workdir: /my/project  |  model: qwen/qwen3-30b-a3b
base_url: https://openrouter.ai/api/v1
[context: .agent_context.md loaded]
Type /help for commands, 'exit' to quit.

> добавь параметр timeout в db.connect() и пробрось его везде
⚡ read_file db.py  → 18 lines, 430 chars
⚡ patch_file db.py  → OK: patched db.py
⚡ grep connect  → 5 lines, 210 chars
⚡ read_file api.py  → 42 lines, 1103 chars
⚡ patch_file api.py  → OK: patched api.py
⚡ read_file config.py  → 12 lines, 280 chars
⚡ patch_file config.py  → OK: patched config.py
Добавил timeout=10 в db.connect(). Пробросил через config.DB_TIMEOUT → api.get_conn().
Все три вызова обновлены.

[14]> /update
[updating context based on this session…]
[updated .agent_context.md]

[14]> exit
[saved session_20260322_214501.json]
Bye!
```
