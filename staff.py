"""
cogs/staff.py

Staff-only slash commands:
  /override  — override a match result and apply Elo
  /cancelm   — cancel any match
  /forcepost — re-post the permanent challenge message
"""

from __future__ import annotations

import os
import logging
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cogs.staff")


def _is_staff(interaction: discord.Interaction) -> bool:
    staff_role_id = os.getenv("STAFF_ROLE_ID")
    if not staff_role_id:
        return False
    role = interaction.guild.get_role(int(staff_role_id))
    return role is not None and role in interaction.user.roles


def staff_check():
    """app_commands check decorator for staff-only commands."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not _is_staff(interaction):
            await interaction.response.send_message(
                "❌ You need the Staff role to use this command.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


class Staff(commands.Cog, name="Staff"):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------
    @app_commands.command(
        name="override",
        description="[Staff] Override a match result and declare the winner.",
    )
    @app_commands.describe(
        match_id="The Match ID to override (without #)",
        winner="The Discord member who won",
        reason="Reason for the override",
    )
    @staff_check()
    async def override(
        self,
        interaction: discord.Interaction,
        match_id: str,
        winner: discord.Member,
        reason: str = "Staff override",
    ):
        match_id = match_id.upper().lstrip("#")
        match = await self.bot.db.get_match(match_id)
        if match is None:
            return await interaction.response.send_message(
                f"❌ Match **#{match_id}** not found.", ephemeral=True
            )
        if match["status"] == "completed":
            return await interaction.response.send_message(
                "❌ This match is already completed.", ephemeral=True
            )
        if match["status"] == "cancelled":
            return await interaction.response.send_message(
                "❌ This match has been cancelled.", ephemeral=True
            )
        if str(winner.id) not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ The winner must be one of the two match participants.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        old_status = match["status"]

        success = await self.bot.db.staff_override(
            match_id,
            staff_id=str(interaction.user.id),
            winner_id=str(winner.id),
            reason=reason,
        )
        if not success:
            return await interaction.followup.send("❌ Override failed.", ephemeral=True)

        match = await self.bot.db.get_match(match_id)

        matches_cog = self.bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.finalise_match(match, interaction.guild, how="staff_override")

        logger = self.bot.get_cog("Logging")
        if logger:
            await logger.log_staff_override(
                interaction.guild, match, interaction.user, old_status
            )

        await interaction.followup.send(
            f"✅ Match **#{match_id}** has been overridden. Winner: {winner.mention}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="cancelm",
        description="[Staff] Cancel any match by ID.",
    )
    @app_commands.describe(
        match_id="The Match ID (without #)",
        reason="Reason for cancellation",
    )
    @staff_check()
    async def cancel_match(
        self,
        interaction: discord.Interaction,
        match_id: str,
        reason: str = "Cancelled by staff",
    ):
        match_id = match_id.upper().lstrip("#")
        match = await self.bot.db.get_match(match_id)
        if match is None:
            return await interaction.response.send_message(
                f"❌ Match **#{match_id}** not found.", ephemeral=True
            )
        if match["status"] in ("completed", "cancelled"):
            return await interaction.response.send_message(
                "❌ Match is already completed or cancelled.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.db.cancel_match(match_id, cancelled_by=str(interaction.user.id), reason=reason)
        match = await self.bot.db.get_match(match_id)

        matches_cog = self.bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(match)

        logger = self.bot.get_cog("Logging")
        if logger:
            await logger.log_match_cancelled(interaction.guild, match, interaction.user)

        await interaction.followup.send(
            f"✅ Match **#{match_id}** has been cancelled.", ephemeral=True
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="forcepost",
        description="[Staff] Re-post the permanent challenge message.",
    )
    @staff_check()
    async def force_post(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Delete old message ID so ensure_challenge_message creates a fresh one
        await self.bot.db.set_config("challenge_message_id", "0")
        challenges_cog = self.bot.get_cog("Challenges")
        if challenges_cog:
            await challenges_cog.ensure_challenge_message()
        await interaction.followup.send(
            "✅ Challenge message re-posted.", ephemeral=True
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="setelo",
        description="[Staff] Manually set a player's Elo rating.",
    )
    @app_commands.describe(user="The Discord member", elo="The new Elo value")
    @staff_check()
    async def set_elo(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        elo: int,
    ):
        if elo < 0 or elo > 9999:
            return await interaction.response.send_message(
                "❌ Elo must be between 0 and 9999.", ephemeral=True
            )
        await self.bot.db.get_or_create_player(str(user.id))
        async with self.bot.db._lock:
            await self.bot.db._db.execute(
                "UPDATE players SET elo = ? WHERE discord_id = ?",
                (elo, str(user.id)),
            )
            await self.bot.db._db.commit()

        logger = self.bot.get_cog("Logging")
        if logger:
            await logger.log_raw(
                interaction.guild,
                title="🔧 Manual Elo Set",
                fields=[
                    ("Staff", interaction.user.mention, True),
                    ("Player", user.mention, True),
                    ("New Elo", str(elo), True),
                ],
                color=discord.Color.orange(),
            )

        await interaction.response.send_message(
            f"✅ {user.mention}'s Elo set to **{elo}**.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Staff(bot))
