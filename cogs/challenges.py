"""
cogs/challenges.py

Handles:
 - Ensuring the permanent challenge message exists in CHALLENGE_CHANNEL_ID.
 - Creating matches + forum posts when a challenge is submitted.
"""

from __future__ import annotations

import asyncio
import os
import uuid
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from views import (
    CreateChallengeView,
    ReportResultView,
    FreeAbilityVoteView,
    GamepassesAbilityVoteView,
    SameAbilityVoteView,
    ABILITY_VOTE_MODES,
    MATCH_MODES,
    REGIONS,
    STATUS_LABELS,
    get_challenge_channel_id,
)

log = logging.getLogger("cogs.challenges")

MATCH_MODE_CHOICES = [
    app_commands.Choice(name=label.split(" — ", 1)[0], value=value)
    for value, label in MATCH_MODES.items()
]
REGION_CHOICES = [
    app_commands.Choice(name=region, value=region)
    for region in REGIONS
]


def _match_embed(match) -> discord.Embed:
    status_label = STATUS_LABELS.get(match["status"], match["status"])
    embed = discord.Embed(
        title="⚔️ Competitive Duel",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Match ID", value=f"#{match['match_id']}", inline=False)
    embed.add_field(
        name="Challenger",
        value=f"Discord: <@{match['challenger_id']}>\nName: **{match['challenger_name']}**",
        inline=True,
    )
    embed.add_field(
        name="Opponent",
        value=f"Discord: <@{match['opponent_id']}>\nName: **{match['opponent_name']}**",
        inline=True,
    )
    embed.add_field(name="Region", value=match["region"], inline=True)

    mode = match["match_mode"] if "match_mode" in match.keys() else "Fist Only"
    mode_icons = {
        "Fist Only":    "👊 Fist Only",
        "Same Ability": "🤝 Same Ability",
        "Free Ability":  "🆓 Free Ability",
        "Gamepasses Ability":  "💎 Gamepasses Ability",
    }
    embed.add_field(name="Mode", value=mode_icons.get(mode, mode), inline=True)

    embed.add_field(name="Status", value=status_label, inline=True)
    return embed


class Challenges(commands.Cog, name="Challenges"):
    def __init__(self, bot):
        self.bot = bot
        self.message_ttl = self._read_message_ttl()
        self._cleanup_task: asyncio.Task | None = None

    @staticmethod
    def _read_message_ttl() -> int:
        """Return the channel message lifetime, with a safe default."""
        raw_ttl = os.getenv("CHALLENGE_MESSAGE_TTL_SECONDS", "60")
        try:
            ttl = int(raw_ttl)
        except ValueError:
            ttl = 60
        return max(1, ttl)

    async def cog_load(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def cog_unload(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()

    def _in_challenge_channel(self, channel_id: int | None) -> bool:
        configured_id = get_challenge_channel_id()
        return configured_id is not None and channel_id == configured_id

    @staticmethod
    def _is_main_challenge_message(
        message: discord.Message, protected_message_id: str | None
    ) -> bool:
        """Keep the permanent GUI safe even if its config entry is missing."""
        if protected_message_id and str(message.id) == protected_message_id:
            return True

        # The permanent panel always contains this stable button custom ID.
        for row in getattr(message, "components", []):
            for component in getattr(row, "children", []):
                if getattr(component, "custom_id", None) == "create_challenge_btn":
                    return True
        return False

    async def _cleanup_loop(self):
        """Keep removing old messages, including messages sent before startup."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._purge_expired_messages()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Could not purge challenge channel messages: %s", exc)
            await asyncio.sleep(min(60, self.message_ttl))

    async def _purge_expired_messages(self):
        channel_id = get_challenge_channel_id()
        if channel_id is None:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as exc:
                log.warning("Could not fetch challenge channel %s: %s", channel_id, exc)
                return

        if not hasattr(channel, "purge"):
            log.warning("Challenge channel %s does not support message purging.", channel_id)
            return

        protected_message_id = await self.bot.db.get_config("challenge_message_id")
        cutoff = discord.utils.utcnow() - timedelta(seconds=self.message_ttl)
        deleted = await channel.purge(
            limit=100,
            before=cutoff,
            check=lambda message: not self._is_main_challenge_message(
                message, protected_message_id
            ),
        )
        if deleted:
            log.info("Purged %d expired message(s) from challenge channel.", len(deleted))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Delete ordinary challenge-channel messages after the configured TTL."""
        if message.guild is None or not self._in_challenge_channel(message.channel.id):
            return

        protected_message_id = await self.bot.db.get_config("challenge_message_id")
        if self._is_main_challenge_message(message, protected_message_id):
            return

        await asyncio.sleep(self.message_ttl)
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except discord.HTTPException as exc:
            log.warning("Could not delete challenge-channel message %s: %s", message.id, exc)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="challenge",
        description="Challenge another player to a ranked duel.",
    )
    @app_commands.describe(
        opponent="The Discord member you want to challenge",
        challenger_name="Your name",
        opponent_name="The opponent's name",
        region="The region for this match",
        match_mode="The rules for this match",
    )
    @app_commands.choices(
        region=REGION_CHOICES,
        match_mode=MATCH_MODE_CHOICES,
    )
    async def challenge(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
        challenger_name: str,
        opponent_name: str,
        region: app_commands.Choice[str],
        match_mode: app_commands.Choice[str],
    ):
        """Create a challenge directly from a slash command."""
        if not self._in_challenge_channel(interaction.channel_id):
            return await interaction.response.send_message(
                "❌ The **/challenge** command can only be used in the challenge channel.",
                ephemeral=True,
            )
        if opponent.bot:
            return await interaction.response.send_message(
                "❌ You cannot challenge a bot.", ephemeral=True
            )
        if opponent.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You cannot challenge yourself.", ephemeral=True
            )

        has_active = await self.bot.db.has_active_match(
            str(interaction.user.id), str(opponent.id)
        )
        if has_active:
            return await interaction.response.send_message(
                "❌ You already have an active match against this opponent. "
                "Resolve it before creating a new challenge.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        match = await self.create_challenge(
            interaction=interaction,
            challenger_name=challenger_name.strip(),
            opponent=opponent,
            opponent_name=opponent_name.strip(),
            region=region.value,
            match_mode=match_mode.value,
        )
        if match is None:
            return await interaction.followup.send(
                "❌ Failed to create challenge. Please try again.", ephemeral=True
            )

        thread_link = (
            f"\n📌 View your match: <#{match['forum_thread_id']}>"
            if match["forum_thread_id"]
            else ""
        )
        await interaction.followup.send(
            f"✅ Challenge created! Match ID: **#{match['match_id']}**\n"
            f"A forum post has been created and {opponent.mention} has been notified."
            f"{thread_link}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    async def ensure_challenge_message(self):
        """
        Checks the database for a stored challenge message ID.
        If it doesn't exist (or is gone), posts a new one.
        """
        channel_id = get_challenge_channel_id()
        if channel_id is None:
            log.warning("CHALLENGE_CHANNEL_ID not set — skipping challenge message.")
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning("Challenge channel %s not found.", channel_id)
            return

        stored_msg_id = await self.bot.db.get_config("challenge_message_id")
        if stored_msg_id:
            try:
                msg = await channel.fetch_message(int(stored_msg_id))
                # Message exists; make sure the view is re-attached
                await msg.edit(view=CreateChallengeView())
                log.info("Challenge message already exists (ID: %s).", stored_msg_id)
                return
            except discord.NotFound:
                log.info("Challenge message was deleted; recreating.")
            except Exception as exc:
                log.exception("Error fetching challenge message: %s", exc)

        # Post a fresh challenge message
        embed = discord.Embed(
            title="⚔️ Want a Competitive Duel?",
            description=(
                "Click the button below to challenge another player to a ranked duel.\n\n"
                "You can also use **/challenge** to select the opponent directly "
                "from Discord.\n\n"
                "You will need:\n"
                "• Your name\n"
                "• Your opponent's name\n"
                "• Your opponent's Discord User ID\n"
                "• Your region"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Duels are ranked — your Elo is on the line!")

        msg = await channel.send(embed=embed, view=CreateChallengeView())
        await self.bot.db.set_config("challenge_message_id", str(msg.id))
        log.info("Posted new challenge message (ID: %s).", msg.id)

    # ------------------------------------------------------------------
    async def create_challenge(
        self,
        interaction: discord.Interaction,
        challenger_name: str,
        opponent: discord.Member,
        opponent_name: str,
        region: str,
        match_mode: str = "Fist Only",
    ):
        """
        Creates a match record and a forum thread.
        Returns the match row, or None on failure.
        """
        match_id = uuid.uuid4().hex[:8].upper()
        match = await self.bot.db.create_match(
            match_id=match_id,
            challenger_id=str(interaction.user.id),
            opponent_id=str(opponent.id),
            challenger_name=challenger_name,
            opponent_name=opponent_name,
            region=region,
            match_mode=match_mode,
        )

        # Ensure both players have a profile
        await self.bot.db.get_or_create_player(str(interaction.user.id))
        await self.bot.db.get_or_create_player(str(opponent.id))

        # Create forum thread
        forum_channel_id = os.getenv("FORUM_CHANNEL_ID")
        if forum_channel_id:
            forum_channel = self.bot.get_channel(int(forum_channel_id))
            if forum_channel and isinstance(forum_channel, discord.ForumChannel):
                try:
                    embed = _match_embed(match)
                    thread_name = f"{challenger_name} vs {opponent_name}"
                    thread, first_msg = await forum_channel.create_thread(
                        name=thread_name,
                        embed=embed,
                        view=ReportResultView(),
                        content=f"⚔️ New duel challenge! {opponent.mention}, you have been challenged!",
                    )
                    await self.bot.db.update_match_forum(
                        match_id=match_id,
                        forum_channel_id=str(forum_channel.id),
                        forum_thread_id=str(thread.id),
                        forum_message_id=str(first_msg.id),
                    )
                    match = await self.bot.db.get_match(match_id)

                    # Post ability vote message for ability-based modes
                    if match_mode in ABILITY_VOTE_MODES:
                        await self._post_ability_vote(thread, match, interaction.user, opponent)

                except Exception as exc:
                    log.exception("Failed to create forum thread: %s", exc)

        # Log the challenge creation
        logger = self.bot.get_cog("Logging")
        if logger:
            await logger.log_challenge_created(interaction.guild, match, interaction.user, opponent)

        return match

    # ------------------------------------------------------------------
    async def _post_ability_vote(
        self,
        thread: discord.Thread,
        match,
        challenger: discord.Member,
        opponent: discord.Member,
    ):
        """Post the ability vote select menu in the forum thread."""
        mode = match["match_mode"]
        if mode == "Free Ability":
            view = FreeAbilityVoteView()
            pool_desc = "**Free abilities** — pick the one you want to play:"
        elif mode == "Gamepasses Ability":
            view = GamepassesAbilityVoteView()
            pool_desc = "**Gamepass abilities** — pick the one you want to play:"
        else:  # Same Ability
            view = SameAbilityVoteView()
            pool_desc = "**All abilities** — both players must vote for the same one:"

        embed = discord.Embed(
            title="🗳️ Ability Vote",
            description=(
                f"Mode: **{mode}**\n\n"
                f"{pool_desc}\n\n"
                f"Both players select their preferred ability below. "
                f"When you both pick the **same** ability it will be locked in."
            ),
            color=discord.Color.blurple(),
        )
        try:
            await thread.send(
                content=f"{challenger.mention} {opponent.mention} — vote for your ability!",
                embed=embed,
                view=view,
            )
        except Exception as exc:
            log.warning("Could not post ability vote message: %s", exc)


async def setup(bot):
    await bot.add_cog(Challenges(bot))
