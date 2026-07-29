"""
cogs/logging_cog.py

Sends structured log embeds to the configured LOG_CHANNEL_ID.
All methods are called by other cogs via bot.get_cog("Logging").
"""

from __future__ import annotations

import os
import logging

import discord
from discord.ext import commands

log = logging.getLogger("cogs.logging_cog")


class Logging(commands.Cog, name="Logging"):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _get_channel(self) -> discord.TextChannel | None:
        channel_id = os.getenv("LOG_CHANNEL_ID")
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception as exc:
                log.warning("Could not fetch log channel: %s", exc)
                return None
        return channel

    async def log_raw(
        self,
        guild: discord.Guild,
        title: str,
        fields: list[tuple[str, str, bool]],
        color: discord.Color = discord.Color.blurple(),
    ):
        """Send a raw embed with arbitrary fields to the log channel."""
        channel = await self._get_channel()
        if channel is None:
            return
        embed = discord.Embed(title=title, color=color)
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        try:
            await channel.send(embed=embed)
        except Exception as exc:
            log.warning("Failed to send log embed: %s", exc)

    # ------------------------------------------------------------------
    # Event-specific log methods
    # ------------------------------------------------------------------
    async def log_challenge_created(
        self,
        guild: discord.Guild,
        match: dict,
        challenger: discord.Member,
        opponent: discord.Member,
    ):
        await self.log_raw(
            guild,
            title="⚔️ Challenge Created",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Challenger", f"{challenger.mention}\n**{match['challenger_roblox']}**", True),
                ("Opponent", f"{opponent.mention}\n**{match['opponent_roblox']}**", True),
                ("Region", match["region"], True),
            ],
            color=discord.Color.blurple(),
        )

    async def log_result_reported(
        self,
        guild: discord.Guild,
        match: dict,
        reporter: discord.Member,
    ):
        await self.log_raw(
            guild,
            title="📋 Result Reported",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Reported By", reporter.mention, True),
                ("Reported Winner", f"<@{match['winner_id']}> (**{match['winner_roblox']}**)", False),
            ],
            color=discord.Color.orange(),
        )

    async def log_result_confirmed(
        self,
        guild: discord.Guild,
        match: dict,
        confirmer: discord.Member,
    ):
        await self.log_raw(
            guild,
            title="✅ Result Confirmed",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Confirmed By", confirmer.mention, True),
                ("Winner", f"<@{match['winner_id']}> (**{match['winner_roblox']}**)", False),
            ],
            color=discord.Color.green(),
        )

    async def log_result_disputed(
        self,
        guild: discord.Guild,
        match: dict,
        disputer: discord.Member,
    ):
        await self.log_raw(
            guild,
            title="🔴 Result Disputed",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Disputed By", disputer.mention, True),
                ("Reported Winner", f"<@{match['winner_id']}> (**{match['winner_roblox']}**)", False),
            ],
            color=discord.Color.red(),
        )

    async def log_match_completed(
        self,
        guild: discord.Guild,
        match: dict,
        how: str,
    ):
        c_before = match["challenger_elo_before"] or "?"
        c_after  = match["challenger_elo_after"]  or "?"
        o_before = match["opponent_elo_before"]   or "?"
        o_after  = match["opponent_elo_after"]    or "?"
        c_diff   = (match["challenger_elo_after"] - match["challenger_elo_before"]) if match["challenger_elo_after"] else 0
        o_diff   = (match["opponent_elo_after"]   - match["opponent_elo_before"])   if match["opponent_elo_after"]  else 0

        await self.log_raw(
            guild,
            title="🏆 Match Completed",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("How", how.replace("_", " ").title(), True),
                ("Winner", f"<@{match['winner_id']}> (**{match['winner_roblox']}**)", False),
                (
                    "Elo Changes",
                    (
                        f"<@{match['challenger_id']}>: {c_before} → {c_after} "
                        f"({'+'if c_diff >= 0 else ''}{c_diff})\n"
                        f"<@{match['opponent_id']}>: {o_before} → {o_after} "
                        f"({'+'if o_diff >= 0 else ''}{o_diff})"
                    ),
                    False,
                ),
            ],
            color=discord.Color.green(),
        )

    async def log_match_cancelled(
        self,
        guild: discord.Guild,
        match: dict,
        cancelled_by: discord.Member,
    ):
        await self.log_raw(
            guild,
            title="⚫ Match Cancelled",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Cancelled By", cancelled_by.mention, True),
                ("Reason", match.get("staff_override_reason") or "No reason given", False),
            ],
            color=discord.Color.dark_gray(),
        )

    async def log_staff_override(
        self,
        guild: discord.Guild,
        match: dict,
        staff: discord.Member,
        old_status: str,
    ):
        await self.log_raw(
            guild,
            title="🔧 Staff Override",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Staff", staff.mention, True),
                ("Previous Status", old_status, True),
                ("Winner Set To", f"<@{match['winner_id']}> (**{match['winner_roblox']}**)", False),
                ("Reason", match.get("staff_override_reason") or "No reason given", False),
            ],
            color=discord.Color.orange(),
        )

    async def log_forfeit_requested(
        self,
        guild: discord.Guild,
        match: dict,
        forfeiter: discord.Member,
    ):
        """Logged when a player voluntarily requests a forfeit."""
        await self.log_raw(
            guild,
            title="🏳️ Forfeit Requested",
            fields=[
                ("Match ID", f"#{match['match_id']}", True),
                ("Forfeiter", forfeiter.mention, True),
                ("Opponent", f"<@{match['challenger_id'] if str(forfeiter.id) == match['opponent_id'] else match['opponent_id']}>", True),
            ],
            color=discord.Color.purple(),
        )

    async def log_forfeit_approved(
        self,
        guild: discord.Guild,
        match: dict,
        staff: discord.Member,
        forfeit_count: int,
    ):
        """Logged when staff approves a forfeit (voluntary or auto)."""
        fields = [
            ("Match ID", f"#{match['match_id']}", True),
            ("Approved By", staff.mention, True),
            ("Forfeiter", f"<@{match['forfeiter_id']}>", True),
            ("Winner", f"<@{match['winner_id']}> (**{match['winner_roblox']}**)", False),
            ("Forfeiter's Total Forfeits", str(forfeit_count), True),
        ]
        if forfeit_count >= 2:
            fields.append(
                ("⚠️ Smurfing / Repeat Alert",
                 f"<@{match['forfeiter_id']}> has forfeited {forfeit_count} time(s). Consider reviewing.",
                 False)
            )
        await self.log_raw(
            guild,
            title="🏳️ Forfeit Approved",
            fields=fields,
            color=discord.Color.purple(),
        )


async def setup(bot):
    await bot.add_cog(Logging(bot))
