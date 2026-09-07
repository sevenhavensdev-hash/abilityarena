"""
cogs/leaderboard.py

Maintains one persistent public leaderboard message. Its buttons open a
private, per-user leaderboard panel so players never overwrite each other's
category, region, or page.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

from views import (
    LeaderboardPersonalView,
    LeaderboardView,
    RegionalLeaderboardView,
)

log = logging.getLogger("cogs.leaderboard")

PAGE_SIZE = 10
WEEK_SECONDS = 7 * 24 * 60 * 60
MONTH_SECONDS = 30 * 24 * 60 * 60
MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}

CATEGORY_TITLES = {
    "overall": "🏆 Current Elo Leaderboard",
    "regional": "🌎 Regional Leaderboard",
    "weekly": "📅 Weekly Leaderboard",
    "monthly": "🗓️ Monthly Leaderboard",
    "all_time": "🏆 All-Time Leaderboard",
}


def _rank_emoji(rank: int) -> str:
    return MEDAL.get(rank, f"**{rank}.**")


class Leaderboard(commands.Cog, name="Leaderboard"):
    def __init__(self, bot):
        self.bot = bot
        self._ensure_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Data and embeds
    # ------------------------------------------------------------------
    async def _rows_for_category(
        self,
        category: str,
        region: str | None = None,
    ) -> list:
        if category == "overall":
            return await self.bot.db.get_leaderboard(limit=1000)

        if category == "regional":
            if not region:
                return []
            return await self.bot.db.get_record_leaderboard(
                region=region,
                limit=1000,
            )

        now = int(time.time())
        if category == "weekly":
            return await self.bot.db.get_record_leaderboard(
                since=now - WEEK_SECONDS,
                limit=1000,
            )
        if category == "monthly":
            return await self.bot.db.get_record_leaderboard(
                since=now - MONTH_SECONDS,
                limit=1000,
            )
        if category == "all_time":
            return await self.bot.db.get_record_leaderboard(limit=1000)

        raise ValueError(f"Unknown leaderboard category: {category}")

    async def _build_embed(
        self,
        category: str,
        page: int = 0,
        region: str | None = None,
    ) -> tuple[discord.Embed, int, int]:
        rows = await self._rows_for_category(category, region)
        total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        start = page * PAGE_SIZE
        page_rows = rows[start : start + PAGE_SIZE]
        title = CATEGORY_TITLES[category]
        if category == "regional" and region:
            title = f"🌎 {region} Leaderboard"

        embed = discord.Embed(title=title, color=discord.Color.gold())

        if category == "overall":
            description = "Ranked by current Elo. Use the buttons below to open your personal view."
        elif category == "regional":
            description = f"Completed-match record for **{region}**."
        elif category == "weekly":
            description = "Completed matches from the last 7 days."
        elif category == "monthly":
            description = "Completed matches from the last 30 days."
        else:
            description = "All completed matches, ranked by wins."

        if not page_rows:
            embed.description = f"{description}\n\nNo players qualify for this leaderboard yet."
        else:
            lines = [description, ""]
            for index, row in enumerate(page_rows):
                rank = start + index + 1
                wins = int(row["wins"])
                losses = int(row["losses"])
                total = wins + losses
                winrate = round(wins / total * 100, 1) if total else 0.0
                lines.append(
                    f"{_rank_emoji(rank)} <@{row['discord_id']}> — **{row['elo']} Elo**\n"
                    f"🏆 {wins} Wins | ❌ {losses} Losses | {winrate}% Win Rate"
                )
            embed.description = "\n\n".join(lines)

        embed.set_footer(
            text=(
                f"Page {page + 1} / {total_pages} • "
                "Your category and page are private to you"
            )
        )
        return embed, page, total_pages

    async def _send_personal(
        self,
        interaction: discord.Interaction,
        category: str,
        region: str | None = None,
        page: int = 0,
        edit: bool = False,
    ):
        embed, page, total_pages = await self._build_embed(
            category=category,
            page=page,
            region=region,
        )
        view = LeaderboardPersonalView(
            category=category,
            region=region,
            page=page,
            total_pages=total_pages,
        )

        if edit:
            await interaction.response.edit_message(
                content=None,
                embed=embed,
                view=view,
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # Private category controls
    # ------------------------------------------------------------------
    async def open_category(self, interaction: discord.Interaction, category: str):
        if category == "regional":
            await interaction.response.send_message(
                "Choose a region for your private leaderboard view.",
                view=RegionalLeaderboardView(),
                ephemeral=True,
            )
            return
        await self._send_personal(interaction, category)

    async def change_personal_category(
        self,
        interaction: discord.Interaction,
        category: str,
    ):
        if category == "regional":
            await interaction.response.edit_message(
                content="Choose a region for your private leaderboard view.",
                embed=None,
                view=RegionalLeaderboardView(),
            )
            return
        await self._send_personal(interaction, category, edit=True)

    async def show_regional(
        self,
        interaction: discord.Interaction,
        region: str,
    ):
        await self._send_personal(
            interaction,
            category="regional",
            region=region,
            edit=True,
        )

    async def paginate_personal(
        self,
        interaction: discord.Interaction,
        view: LeaderboardPersonalView,
        direction: int,
    ):
        await self._send_personal(
            interaction,
            category=view.category,
            region=view.region,
            page=view.page + direction,
            edit=True,
        )

    # ------------------------------------------------------------------
    # Persistent public message
    # ------------------------------------------------------------------
    async def _channel_from_id(self, channel_id: str | None):
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.DiscordException as exc:
                log.warning("Could not fetch leaderboard channel %s: %s", channel_id, exc)
        return channel

    async def _create_public_message(self, channel):
        embed, _, _ = await self._build_embed("overall")
        message = await channel.send(embed=embed, view=LeaderboardView())
        await self.bot.db.set_config("leaderboard_channel_id", str(channel.id))
        await self.bot.db.set_config("leaderboard_message_id", str(message.id))
        log.info("Created persistent leaderboard message %s in channel %s.", message.id, channel.id)
        return message

    async def _ensure_leaderboard_message(self, fallback_channel=None):
        """Find the public leaderboard message or recreate it if deleted."""
        configured_channel_id = os.getenv("LEADERBOARD_CHANNEL_ID")
        stored_channel_id = await self.bot.db.get_config("leaderboard_channel_id")
        target_channel_id = configured_channel_id or stored_channel_id
        channel = await self._channel_from_id(target_channel_id)
        if channel is None and not target_channel_id:
            channel = fallback_channel
        if channel is None:
            log.info(
                "Leaderboard target channel is unavailable. Set LEADERBOARD_CHANNEL_ID "
                "or use /leaderboard after choosing the target channel."
            )
            return None

        message_id = await self.bot.db.get_config("leaderboard_message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                embed, _, _ = await self._build_embed("overall")
                await message.edit(embed=embed, view=LeaderboardView())
                await self.bot.db.set_config("leaderboard_channel_id", str(channel.id))
                return message
            except discord.NotFound:
                log.info("Leaderboard message %s is gone; recreating it.", message_id)
            except discord.DiscordException as exc:
                log.warning("Could not refresh leaderboard message %s: %s", message_id, exc)
                return None

        try:
            return await self._create_public_message(channel)
        except discord.DiscordException as exc:
            log.warning("Could not create leaderboard message: %s", exc)
            return None

    async def ensure_leaderboard_message(self, fallback_channel=None):
        """Serialize setup so concurrent commands cannot create duplicate messages."""
        async with self._ensure_lock:
            return await self._ensure_leaderboard_message(fallback_channel)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        message_id = await self.bot.db.get_config("leaderboard_message_id")
        if message_id == str(payload.message_id):
            await self.ensure_leaderboard_message()

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        message_id = await self.bot.db.get_config("leaderboard_message_id")
        if message_id and int(message_id) in payload.message_ids:
            await self.ensure_leaderboard_message()

    # ------------------------------------------------------------------
    @app_commands.command(
        name="leaderboard",
        description="Show the persistent competitive leaderboard.",
    )
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.ensure_leaderboard_message(
            fallback_channel=interaction.channel,
        )
        if message is None:
            await interaction.followup.send(
                "❌ I could not create or find the leaderboard message in its target channel.",
                ephemeral=True,
            )
            return

        embed, page, total_pages = await self._build_embed("overall")
        await interaction.followup.send(
            f"✅ The leaderboard is ready. Public post: {message.jump_url}",
            embed=embed,
            view=LeaderboardPersonalView(
                category="overall",
                page=page,
                total_pages=total_pages,
            ),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
