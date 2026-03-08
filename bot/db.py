"""
Работа с базой данных (aiosqlite).

Таблицы:
  servers — список удалённых серверов
  results — история проверок (агрегат + список проблемных SNI)
"""

import json
import aiosqlite
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
#  Инициализация
# ══════════════════════════════════════════════════════════════════════════════

async def init_db(db: aiosqlite.Connection) -> None:
    await db.executescript("""
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS servers (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        ip              TEXT    NOT NULL,
        ssh_credentials TEXT    NOT NULL,
        status          TEXT    NOT NULL DEFAULT 'unknown',
        last_check_time TEXT
    );

    CREATE TABLE IF NOT EXISTS results (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id    INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
        checked_at   TEXT    NOT NULL,
        total        INTEGER NOT NULL DEFAULT 0,
        working      INTEGER NOT NULL DEFAULT 0,
        blocked      INTEGER NOT NULL DEFAULT 0,
        inconclusive INTEGER NOT NULL DEFAULT 0,
        elapsed_sec  REAL    NOT NULL DEFAULT 0,
        min_rtt      INTEGER,
        avg_rtt      REAL,
        max_rtt      INTEGER,
        blocked_snis TEXT    NOT NULL DEFAULT '[]'
    );

    CREATE INDEX IF NOT EXISTS idx_results_server
        ON results(server_id, checked_at DESC);
    """)
    await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  Серверы
# ══════════════════════════════════════════════════════════════════════════════

async def add_server(db: aiosqlite.Connection,
                     name: str, ip: str, cred: str) -> int:
    cur = await db.execute(
        "INSERT INTO servers (name, ip, ssh_credentials) VALUES (?, ?, ?)",
        (name, ip, cred),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def get_servers(db: aiosqlite.Connection) -> list[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT id, name, ip, ssh_credentials, status, last_check_time "
        "FROM servers ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def get_server(db: aiosqlite.Connection,
                     server_id: int) -> Optional[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT id, name, ip, ssh_credentials, status, last_check_time "
        "FROM servers WHERE id = ?",
        (server_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def delete_server(db: aiosqlite.Connection, server_id: int) -> None:
    await db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    await db.commit()


async def update_server_status(db: aiosqlite.Connection,
                                server_id: int,
                                status: str,
                                ts: str) -> None:
    await db.execute(
        "UPDATE servers SET status = ?, last_check_time = ? WHERE id = ?",
        (status, ts, server_id),
    )
    await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  История результатов
# ══════════════════════════════════════════════════════════════════════════════

async def save_result(db: aiosqlite.Connection, *,
                      server_id:    int,
                      checked_at:   str,
                      total:        int,
                      working:      int,
                      blocked:      int,
                      inconclusive: int,
                      elapsed_sec:  float,
                      min_rtt:      Optional[int],
                      avg_rtt:      Optional[float],
                      max_rtt:      Optional[int],
                      blocked_snis: list) -> int:
    """Сохраняет запись о прогоне. Возвращает id новой записи."""
    cur = await db.execute(
        """
        INSERT INTO results
            (server_id, checked_at, total, working, blocked, inconclusive,
             elapsed_sec, min_rtt, avg_rtt, max_rtt, blocked_snis)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            server_id, checked_at, total, working, blocked, inconclusive,
            round(elapsed_sec, 2),
            min_rtt,
            round(avg_rtt, 1) if avg_rtt is not None else None,
            max_rtt,
            json.dumps(blocked_snis, ensure_ascii=False),
        ),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def get_recent_results(db: aiosqlite.Connection,
                              server_id: int,
                              limit: int = 10) -> list[dict]:
    """Последние N результатов для сервера (новые первыми)."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT id, server_id, checked_at, total, working, blocked,
               inconclusive, elapsed_sec, min_rtt, avg_rtt, max_rtt, blocked_snis
        FROM results
        WHERE server_id = ?
        ORDER BY checked_at DESC
        LIMIT ?
        """,
        (server_id, limit),
    ) as cur:
        rows = await cur.fetchall()

    out = []
    for r in rows:
        d = dict(r)
        try:
            d["blocked_snis"] = json.loads(d["blocked_snis"] or "[]")
        except (json.JSONDecodeError, TypeError):
            d["blocked_snis"] = []
        out.append(d)
    return out


async def get_result(db: aiosqlite.Connection,
                     result_id: int) -> Optional[dict]:
    """Получить одну запись истории по ID."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM results WHERE id = ?", (result_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["blocked_snis"] = json.loads(d["blocked_snis"] or "[]")
    except (json.JSONDecodeError, TypeError):
        d["blocked_snis"] = []
    return d


async def prune_old_results(db: aiosqlite.Connection,
                             server_id: int,
                             keep: int = 20) -> None:
    """Удаляет записи сверх лимита keep (оставляет последние)."""
    await db.execute(
        """
        DELETE FROM results
        WHERE server_id = ?
          AND id NOT IN (
              SELECT id FROM results
              WHERE server_id = ?
              ORDER BY checked_at DESC
              LIMIT ?
          )
        """,
        (server_id, server_id, keep),
    )
    await db.commit()
