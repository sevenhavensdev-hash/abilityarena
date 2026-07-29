"""
cogs/leaderboard.py

Handles the paginated leaderboard.
/leaderboard — sends or updates the leaderboard embed.

The leaderboard always reads live data from the database, so any stat
resets (via /resetwins, /resetlosses, /resetelo, /resetall) are
automatically reflected the next time someone views or refreshes it.
"""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

from views import LeaderboardView

log = logging.getLogger("cogs.leaderboard")

PAGE_SIZE = 10
MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def _rank_emoji(rank: int) -> str:
    return MEDAL.get(rank, f"**{rank}.**")


class Leaderboard(commands.Cog, name="Leaderboard"):
    def __init__(self, bot):
        self.bot = bot
        # Track per-message pagination state: message_id -> page_index
        self._pages: dict[int, int] = {}

    # ------------------------------------------------------------------
    async def _build_embed(self, page: int) -> tuple[discord.Embed, int, int]:
        """Returns (embed, current_page, total_pages)."""
        rows = await self.bot.db.get_leaderboard(limit=1000)
        total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        start = page * PAGE_SIZE
        slice_ = rows[start : start + PAGE_SIZE]

        embed = discord.Embed(
            title="🏆 Competitive Leaderboard",
            color=discord.Color.gold(),
        )

        if not slice_:
            embed.description = "No players yet. Be the first to duel!"
        else:
            lines = []
            for i, row in enumerate(slice_):
                rank    = start + i + 1
                wins    = row["wins"]
                losses  = row["losses"]
                total   = wins + losses
                winrate = round(wins / total * 100, 1) if total else 0.0
                lines.append(
                    f"{_rank_emoji(rank)} <@{row['discord_id']}> — **{row['elo']} Elo**\n"
                    f"🏆 {wins} Wins | ❌ {losses} Losses | {winrate}% Win Rate"
                )
            embed.description = "\n\n".join(lines)

        embed.set_footer(text=f"Page {page + 1} / {total_pages}")
        return embed, page, total_pages

    # ------------------------------------------------------------------
    async def paginate(
        self, interaction: discord.Interaction, direction: int
    ):
        """Called by LeaderboardView buttons (prev / next / refresh)."""
        msg_id = interaction.message.id if interaction.message else None
        current_page = self._pages.get(msg_id, 0)
        new_page = current_page + direction

        embed, new_page, _ = await self._build_embed(new_page)
        if msg_id is not None:
            self._pages[msg_id] = new_page

        await interaction.response.edit_message(embed=embed, view=LeaderboardView())

    # ------------------------------------------------------------------
    @app_commands.command(
        name="leaderboard", description="View the competitive Elo leaderboard."
    )
    async def leaderboard(self, interaction: discord.Interaction):
        embed, _, total_pages = await self._build_embed(0)
        view = LeaderboardView() if total_pages > 1 else discord.ui.View()
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        self._pages[msg.id] = 0


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
