"""
database.py — SQLite database layer (async via aiosqlite).
All SQL lives here; cogs call the methods on bot.db.
"""

import os
import time
import asyncio
import logging
from typing import Optional

import aiosqlite

log = logging.getLogger("database")

DB_PATH = os.getenv("DB_PATH", "database.db")


class Database:
    def __init__(self):
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self):
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()

        # Migrations — safe to run on every start; silently skip if column exists
        migrations = [
            "ALTER TABLE matches ADD COLUMN dispute_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE matches ADD COLUMN forfeiter_id TEXT",
            "ALTER TABLE matches ADD COLUMN forfeit_notified INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE players ADD COLUMN forfeit_count INTEGER NOT NULL DEFAULT 0",
        ]
        for stmt in migrations:
            try:
                await self._db.execute(stmt)
                log.info("Migration applied: %s", stmt[:60])
            except Exception:
                pass  # Column already exists — ignore

        await self._db.commit()
        log.info("Database initialised at %s", DB_PATH)

    async def close(self):
        if self._db:
            await self._db.close()

    # ------------------------------------------------------------------
    # Table creation
    # ------------------------------------------------------------------
    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                discord_id     TEXT PRIMARY KEY,
                elo            INTEGER NOT NULL DEFAULT 1200,
                wins           INTEGER NOT NULL DEFAULT 0,
                losses         INTEGER NOT NULL DEFAULT 0,
                forfeit_count  INTEGER NOT NULL DEFAULT 0,
                created_at     INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS matches (
                match_id              TEXT PRIMARY KEY,
                challenger_id         TEXT NOT NULL,
                opponent_id           TEXT NOT NULL,
                challenger_roblox     TEXT NOT NULL,
                opponent_roblox       TEXT NOT NULL,
                region                TEXT NOT NULL,
                status                TEXT NOT NULL DEFAULT 'awaiting',
                winner_id             TEXT,
                winner_roblox         TEXT,
                loser_id              TEXT,
                loser_roblox          TEXT,
                reporter_id           TEXT,
                confirmer_id          TEXT,
                staff_override_id     TEXT,
                staff_override_reason TEXT,
                challenger_elo_before INTEGER,
                opponent_elo_before   INTEGER,
                challenger_elo_after  INTEGER,
                opponent_elo_after    INTEGER,
                elo_changed           INTEGER NOT NULL DEFAULT 0,
                dispute_count         INTEGER NOT NULL DEFAULT 0,
                forfeiter_id          TEXT,
                forfeit_notified      INTEGER NOT NULL DEFAULT 0,
                forum_channel_id      TEXT,
                forum_thread_id       TEXT,
                forum_message_id      TEXT,
                created_at            INTEGER NOT NULL,
                completed_at          INTEGER
            );

            CREATE TABLE IF NOT EXISTS bot_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    def _now(self) -> int:
        return int(time.time())

    # ------------------------------------------------------------------
    # Player methods
    # ------------------------------------------------------------------
    async def get_or_create_player(self, discord_id: str) -> aiosqlite.Row:
        async with self._lock:
            async with self._db.execute(
                "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await self._db.execute(
                    "INSERT INTO players (discord_id, elo, wins, losses, forfeit_count, created_at) "
                    "VALUES (?, 1200, 0, 0, 0, ?)",
                    (discord_id, self._now()),
                )
                await self._db.commit()
                async with self._db.execute(
                    "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
                ) as cur:
                    row = await cur.fetchone()
        return row

    async def get_player(self, discord_id: str) -> Optional[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return await cur.fetchone()

    async def update_player_elo(
        self, discord_id: str, new_elo: int, won: bool
    ):
        new_elo = min(3000, max(0, new_elo))  # hard cap: 0–3000
        async with self._lock:
            if won:
                await self._db.execute(
                    "UPDATE players SET elo = ?, wins = wins + 1 WHERE discord_id = ?",
                    (new_elo, discord_id),
                )
            else:
                await self._db.execute(
                    "UPDATE players SET elo = ?, losses = losses + 1 WHERE discord_id = ?",
                    (new_elo, discord_id),
                )
            await self._db.commit()

    async def get_leaderboard(self, limit: int = 100) -> list:
        async with self._db.execute(
            "SELECT * FROM players ORDER BY elo DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()

    async def get_player_forfeit_count(self, discord_id: str) -> int:
        """Returns the total number of forfeits recorded for this player."""
        async with self._db.execute(
            "SELECT COALESCE(forfeit_count, 0) AS fc FROM players WHERE discord_id = ?",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row["fc"]) if row else 0

    # ------------------------------------------------------------------
    # Match methods
    # ------------------------------------------------------------------
    async def create_match(
        self,
        match_id: str,
        challenger_id: str,
        opponent_id: str,
        challenger_roblox: str,
        opponent_roblox: str,
        region: str,
    ) -> aiosqlite.Row:
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO matches
                    (match_id, challenger_id, opponent_id, challenger_roblox,
                     opponent_roblox, region, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting', ?)
                """,
                (
                    match_id,
                    challenger_id,
                    opponent_id,
                    challenger_roblox,
                    opponent_roblox,
                    region,
                    self._now(),
                ),
            )
            await self._db.commit()
        return await self.get_match(match_id)

    async def get_match(self, match_id: str) -> Optional[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ) as cur:
            return await cur.fetchone()

    async def update_match_forum(
        self,
        match_id: str,
        forum_channel_id: str,
        forum_thread_id: str,
        forum_message_id: str,
    ):
        async with self._lock:
            await self._db.execute(
                """
                UPDATE matches
                SET forum_channel_id = ?, forum_thread_id = ?, forum_message_id = ?
                WHERE match_id = ?
                """,
                (forum_channel_id, forum_thread_id, forum_message_id, match_id),
            )
            await self._db.commit()

    async def update_match_status(self, match_id: str, status: str):
        async with self._lock:
            await self._db.execute(
                "UPDATE matches SET status = ? WHERE match_id = ?",
                (status, match_id),
            )
            await self._db.commit()

    async def report_result(
        self, match_id: str, reporter_id: str, winner_id: str
    ) -> bool:
        """Set the preliminary winner. Returns False if already reported."""
        match = await self.get_match(match_id)
        if match is None or match["reporter_id"] is not None:
            return False
        # Determine winner/loser roblox names
        if winner_id == match["challenger_id"]:
            winner_roblox = match["challenger_roblox"]
            loser_id = match["opponent_id"]
            loser_roblox = match["opponent_roblox"]
        else:
            winner_roblox = match["opponent_roblox"]
            loser_id = match["challenger_id"]
            loser_roblox = match["challenger_roblox"]

        async with self._lock:
            await self._db.execute(
                """
                UPDATE matches
                SET reporter_id = ?, winner_id = ?, winner_roblox = ?,
                    loser_id = ?, loser_roblox = ?, status = 'awaiting_confirm'
                WHERE match_id = ?
                """,
                (
                    reporter_id,
                    winner_id,
                    winner_roblox,
                    loser_id,
                    loser_roblox,
                    match_id,
                ),
            )
            await self._db.commit()
        return True

    async def confirm_result(self, match_id: str, confirmer_id: str) -> bool:
        """Mark match confirmed. Returns False if already done."""
        match = await self.get_match(match_id)
        if match is None or match["confirmer_id"] is not None:
            return False
        if match["reporter_id"] == confirmer_id:
            return False  # cannot confirm own report
        async with self._lock:
            await self._db.execute(
                "UPDATE matches SET confirmer_id = ?, status = 'in_progress' WHERE match_id = ?",
                (confirmer_id, match_id),
            )
            await self._db.commit()
        return True

    async def dispute_result(self, match_id: str, disputer_id: str) -> int:
        """Increment dispute count. Sets status to 'disputed' only on the 2nd dispute.
        Returns the new dispute_count."""
        async with self._lock:
            await self._db.execute(
                "UPDATE matches SET dispute_count = dispute_count + 1 WHERE match_id = ?",
                (match_id,),
            )
            await self._db.commit()
        match = await self.get_match(match_id)
        count = match["dispute_count"]
        if count >= 2:
            async with self._lock:
                await self._db.execute(
                    "UPDATE matches SET status = 'disputed' WHERE match_id = ?",
                    (match_id,),
                )
                await self._db.commit()
        return count

    async def apply_elo(
        self,
        match_id: str,
        challenger_elo_before: int,
        opponent_elo_before: int,
        challenger_elo_after: int,
        opponent_elo_after: int,
    ) -> bool:
        """Persist final elo changes (idempotency via elo_changed flag)."""
        match = await self.get_match(match_id)
        if match is None or match["elo_changed"]:
            return False
        async with self._lock:
            await self._db.execute(
                """
                UPDATE matches
                SET elo_changed = 1,
                    challenger_elo_before = ?,
                    opponent_elo_before = ?,
                    challenger_elo_after = ?,
                    opponent_elo_after = ?,
                    status = 'completed',
                    completed_at = ?
                WHERE match_id = ?
                """,
                (
                    challenger_elo_before,
                    opponent_elo_before,
                    challenger_elo_after,
                    opponent_elo_after,
                    self._now(),
                    match_id,
                ),
            )
            await self._db.commit()
        return True

    async def staff_override(
        self,
        match_id: str,
        staff_id: str,
        winner_id: str,
        reason: str,
    ) -> bool:
        match = await self.get_match(match_id)
        if match is None:
            return False
        # Determine roblox names
        if winner_id == match["challenger_id"]:
            winner_roblox = match["challenger_roblox"]
            loser_id = match["opponent_id"]
            loser_roblox = match["opponent_roblox"]
        else:
            winner_roblox = match["opponent_roblox"]
            loser_id = match["challenger_id"]
            loser_roblox = match["challenger_roblox"]
        async with self._lock:
            await self._db.execute(
                """
                UPDATE matches
                SET staff_override_id = ?,
                    staff_override_reason = ?,
                    winner_id = ?,
                    winner_roblox = ?,
                    loser_id = ?,
                    loser_roblox = ?,
                    confirmer_id = ?
                WHERE match_id = ?
                """,
                (
                    staff_id,
                    reason,
                    winner_id,
                    winner_roblox,
                    loser_id,
                    loser_roblox,
                    staff_id,
                    match_id,
                ),
            )
            await self._db.commit()
        return True

    async def cancel_match(self, match_id: str, cancelled_by: str, reason: str = ""):
        async with self._lock:
            await self._db.execute(
                """
                UPDATE matches
                SET status = 'cancelled',
                    staff_override_reason = ?,
                    staff_override_id = ?,
                    completed_at = ?
                WHERE match_id = ?
                """,
                (reason or "Cancelled", cancelled_by, self._now(), match_id),
            )
            await self._db.commit()

    async def has_active_match(
        self, challenger_id: str, opponent_id: str
    ) -> bool:
        """Check if an active (non-completed/cancelled) match exists between two players."""
        active = ("awaiting", "in_progress", "awaiting_confirm", "disputed", "awaiting_forfeit")
        placeholders = ",".join("?" * len(active))
        async with self._db.execute(
            f"""
            SELECT 1 FROM matches
            WHERE status IN ({placeholders})
              AND (
                    (challenger_id = ? AND opponent_id = ?)
                 OR (challenger_id = ? AND opponent_id = ?)
              )
            LIMIT 1
            """,
            (*active, challenger_id, opponent_id, opponent_id, challenger_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_player_matches(self, discord_id: str, limit: int = 10) -> list:
        async with self._db.execute(
            """
            SELECT * FROM matches
            WHERE (challenger_id = ? OR opponent_id = ?)
              AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (discord_id, discord_id, limit),
        ) as cur:
            return await cur.fetchall()

    # ------------------------------------------------------------------
    # Forfeit methods
    # ------------------------------------------------------------------
    async def request_forfeit(self, match_id: str, forfeiter_id: str) -> bool:
        """
        Mark a match as awaiting forfeit review.
        Only valid if the match is currently 'awaiting' or 'in_progress'.
        Returns False if the match is not in a forfeitable state.
        """
        match = await self.get_match(match_id)
        if match is None or match["status"] not in ("awaiting", "in_progress"):
            return False
        async with self._lock:
            await self._db.execute(
                "UPDATE matches SET status = 'awaiting_forfeit', forfeiter_id = ? WHERE match_id = ?",
                (forfeiter_id, match_id),
            )
            await self._db.commit()
        return True

    async def approve_forfeit(self, match_id: str) -> bool:
        """
        Approve a pending forfeit. Sets winner to the non-forfeiter,
        stores 'Forfeit' in staff_override_reason, increments forfeit_count
        on the forfeiter's player record.
        """
        match = await self.get_match(match_id)
        if match is None or match["status"] != "awaiting_forfeit":
            return False

        forfeiter_id = match["forfeiter_id"]
        if forfeiter_id is None:
            return False

        if forfeiter_id == match["challenger_id"]:
            winner_id    = match["opponent_id"]
            winner_roblox = match["opponent_roblox"]
            loser_id     = match["challenger_id"]
            loser_roblox  = match["challenger_roblox"]
        else:
            winner_id    = match["challenger_id"]
            winner_roblox = match["challenger_roblox"]
            loser_id     = match["opponent_id"]
            loser_roblox  = match["opponent_roblox"]

        async with self._lock:
            await self._db.execute(
                """
                UPDATE matches
                SET winner_id = ?, winner_roblox = ?,
                    loser_id = ?, loser_roblox = ?,
                    reporter_id = ?, confirmer_id = ?,
                    staff_override_reason = 'Forfeit'
                WHERE match_id = ?
                """,
                (
                    winner_id, winner_roblox,
                    loser_id,  loser_roblox,
                    forfeiter_id, forfeiter_id,
                    match_id,
                ),
            )
            # Increment the forfeiter's lifetime forfeit counter
            await self._db.execute(
                "UPDATE players SET forfeit_count = COALESCE(forfeit_count, 0) + 1 "
                "WHERE discord_id = ?",
                (forfeiter_id,),
            )
            await self._db.commit()
        return True

    async def deny_forfeit(self, match_id: str) -> bool:
        """
        Deny a pending forfeit request. Reverts match status to 'awaiting'
        and clears the forfeiter_id.
        """
        async with self._lock:
            await self._db.execute(
                "UPDATE matches SET status = 'awaiting', forfeiter_id = NULL WHERE match_id = ?",
                (match_id,),
            )
            await self._db.commit()
        return True

    async def get_stale_matches(self, seconds: int = 259200) -> list:
        """
        Return matches that have been in 'awaiting' status for longer than
        `seconds` (default 72 h = 3 days) and have not yet had an auto-forfeit
        notification sent.
        """
        cutoff = self._now() - seconds
        async with self._db.execute(
            """
            SELECT * FROM matches
            WHERE status = 'awaiting'
              AND forfeit_notified = 0
              AND created_at < ?
            """,
            (cutoff,),
        ) as cur:
            return await cur.fetchall()

    async def mark_forfeit_notified(self, match_id: str):
        """Flag a match so the auto-forfeit task won't re-notify for it."""
        async with self._lock:
            await self._db.execute(
                "UPDATE matches SET forfeit_notified = 1 WHERE match_id = ?",
                (match_id,),
            )
            await self._db.commit()

    # ------------------------------------------------------------------
    # Bot config (persistent key/value store)
    # ------------------------------------------------------------------
    async def get_config(self, key: str) -> Optional[str]:
        async with self._db.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set_config(self, key: str, value: str):
        async with self._lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)",
                (key, value),
            )
            await self._db.commit()
