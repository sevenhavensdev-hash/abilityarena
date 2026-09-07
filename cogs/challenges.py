"""
cogs/challenges.py

Handles:
 - Ensuring the permanent challenge message exists in CHALLENGE_CHANNEL_ID.
 - Creating matches + forum posts when a challenge is submitted.
"""

from __future__ import annotations

import os
import uuid
import logging

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
        value=f"Discord: <@{match['challenger_id']}>\nRoblox: **{match['challenger_roblox']}**",
        inline=True,
    )
    embed.add_field(
        name="Opponent",
        value=f"Discord: <@{match['opponent_id']}>\nRoblox: **{match['opponent_roblox']}**",
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

    # ------------------------------------------------------------------
    @app_commands.command(
        name="challenge",
        description="Challenge another player to a ranked duel.",
    )
    @app_commands.describe(
        opponent="The Discord member you want to challenge",
        challenger_roblox="Your Roblox username",
        opponent_roblox="The opponent's Roblox username",
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
        challenger_roblox: str,
        opponent_roblox: str,
        region: app_commands.Choice[str],
        match_mode: app_commands.Choice[str],
    ):
        """Create a challenge directly from a slash command."""
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
            challenger_roblox=challenger_roblox.strip(),
            opponent=opponent,
            opponent_roblox=opponent_roblox.strip(),
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
        channel_id = os.getenv("CHALLENGE_CHANNEL_ID")
        if not channel_id:
            log.warning("CHALLENGE_CHANNEL_ID not set — skipping challenge message.")
            return

        channel = self.bot.get_channel(int(channel_id))
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
                "• Your Roblox username\n"
                "• Your opponent's Roblox username\n"
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
        challenger_roblox: str,
        opponent: discord.Member,
        opponent_roblox: str,
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
            challenger_roblox=challenger_roblox,
            opponent_roblox=opponent_roblox,
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
                    thread_name = f"{challenger_roblox} vs {opponent_roblox}"
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
