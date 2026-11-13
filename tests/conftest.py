from __future__ import annotations

import pytest

from virtudoc.config import DATA_DIR_ENV
from virtudoc.db import open_db
from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setenv(DATA_DIR_ENV, str(path))
    return path


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "index.db")
    yield connection
    connection.close()


GUIDE_MD = """---
title: Guide
---
# Руководство

Введение в библиотеку и общие сведения.

## Установка

Установите пакет через pip и проверьте версию.

### Требования

Нужен Python 3.11 или новее.

## API

Основные классы и функции.

### Middleware

Функция use принимает middleware и добавляет её в конвейер обработки.
Каждая middleware получает контекст и вызывает следующий обработчик.

### Ошибки

Иерархия исключений библиотеки.
"""

API_MD = """# API Reference

## Маршрутизация

Роутер сопоставляет пути с обработчиками.
"""

PLAIN_TXT = """Контракт очереди сообщений

Очередь доставляет события подписчикам с гарантией порядка.
"""


@pytest.fixture
def docs_dir(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(GUIDE_MD, encoding="utf-8")
    (docs / "api.md").write_text(API_MD, encoding="utf-8")
    (docs / "queue.txt").write_text(PLAIN_TXT, encoding="utf-8")
    hidden = docs / ".git"
    hidden.mkdir()
    (hidden / "internals.md").write_text("# Служебное\n\nСкрытый файл.\n", encoding="utf-8")
    (docs / "style.css").write_text("body {}", encoding="utf-8")
    return docs


@pytest.fixture
def indexed(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo", version="1.0")
    stats = index_library(conn, lib_id, Chunker())
    return lib_id, stats
