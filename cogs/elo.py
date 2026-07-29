"""
cogs/elo.py

Elo calculation logic.
Base gain/loss: 30 Elo per match.
Adjusted slightly based on rating difference (underdog gets more, favourite gets less).
"""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cogs.elo")


class Elo(commands.Cog, name="Elo"):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------
    def calculate(
        self, winner_elo: int, loser_elo: int
    ) -> tuple[int, int]:
        """
        Returns (new_winner_elo, new_loser_elo).

        Base gain/loss is always 30.
        Bonus/penalty added based on rating difference:
          - Beating someone stronger  → gain more than 30 (up to +40)
          - Beating someone weaker    → gain less than 30 (down to +20)
        Winner always gains what the loser loses (zero-sum).
        """
        diff = loser_elo - winner_elo  # positive = underdog win, negative = favourite win

        # Scale diff (clamped to ±200) into a ±10 bonus
        clamped = max(-200, min(200, diff))
        bonus = round(clamped / 200 * 10)  # -10 to +10

        elo_change = max(20, min(40, 30 + bonus))

        new_winner = min(3000, winner_elo + elo_change)
        new_loser  = max(100, loser_elo - elo_change)

        return new_winner, new_loser

    # ------------------------------------------------------------------
    @app_commands.command(name="elo", description="View your Elo rating and stats.")
    @app_commands.describe(user="The user to look up (leave blank for yourself)")
    async def view_elo(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ):
        target = user or interaction.user
        player = await self.bot.db.get_player(str(target.id))
        if player is None:
            if target == interaction.user:
                msg = "You have no rating yet. Create a challenge to get started!"
            else:
                msg = f"{target.display_name} has no rating yet."
            return await interaction.response.send_message(msg, ephemeral=True)

        wins     = player["wins"]
        losses   = player["losses"]
        total    = wins + losses
        winrate  = round(wins / total * 100, 1) if total else 0.0
        forfeits = player["forfeit_count"] if "forfeit_count" in player.keys() else 0

        embed = discord.Embed(
            title=f"⚔️ {target.display_name}'s Rating",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Elo",      value=str(player["elo"]), inline=True)
        embed.add_field(name="Wins",     value=str(wins),           inline=True)
        embed.add_field(name="Losses",   value=str(losses),         inline=True)
        embed.add_field(name="Win Rate", value=f"{winrate}%",       inline=True)
        if forfeits:
            embed.add_field(name="Forfeits", value=str(forfeits),   inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Elo(bot))
