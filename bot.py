"""
Competitive Roblox Duel Discord Bot
Entry point — loads cogs and initialises persistent views.
"""

import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import Database
from views import (
    CreateChallengeView,
    ReportResultView,
    ConfirmResultView,
    StaffOverrideView,
    LeaderboardView,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Logging (to stdout, Wispbyte captures it)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class DuelBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.db: Database = Database()
        self._ready_fired: bool = False  # guard against repeated on_ready calls

    async def setup_hook(self):
        await self.db.initialize()

        # Register persistent views so buttons survive restarts
        self.add_view(CreateChallengeView())
        self.add_view(ReportResultView())
        self.add_view(ConfirmResultView())
        self.add_view(StaffOverrideView())
        self.add_view(LeaderboardView())

        # Load all cogs
        cog_list = [
            "cogs.challenges",
            "cogs.matches",
            "cogs.elo",
            "cogs.leaderboard",
            "cogs.staff",
            "cogs.logging_cog",
        ]
        for cog in cog_list:
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)

        # Sync slash commands
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally")

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="⚔️ Competitive Duels",
            )
        )
        # on_ready fires on every reconnect; only run first-time setup once
        if self._ready_fired:
            log.info("on_ready fired again (reconnect) — skipping challenge message setup.")
            return
        self._ready_fired = True
        ch = self.get_cog("Challenges")
        if ch:
            await ch.ensure_challenge_message()

    async def on_error(self, event: str, *args, **kwargs):
        log.exception("Unhandled error in event %s", event)


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot = DuelBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
