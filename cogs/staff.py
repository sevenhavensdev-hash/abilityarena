"""
cogs/staff.py

Staff-only slash commands:
  /override    — override a match result and apply Elo
  /cancelm     — cancel any match
  /forcepost   — re-post the permanent challenge message
  /setelo      — manually set a player's Elo (admin only)
  /resetwins   — reset a player's win count to 0
  /resetlosses — reset a player's loss count to 0
  /resetelo    — reset a player's Elo to 1200
  /resetall    — reset a player's wins, losses, forfeit count, and Elo
"""

from __future__ import annotations

import os
import logging
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cogs.staff")


def _is_staff(interaction: discord.Interaction) -> bool:
    # Anyone with Discord administrator permission is automatically staff
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = os.getenv("STAFF_ROLE_ID")
    if not staff_role_id:
        return False
    role = interaction.guild.get_role(int(staff_role_id))
    return role is not None and role in interaction.user.roles


def _is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    raw = os.getenv("ADMIN_ROLE_IDS") or os.getenv("ADMIN_ROLE_ID", "")
    admin_role_ids = {rid.strip() for rid in raw.split(",") if rid.strip()}
    if not admin_role_ids:
        return False
    user_role_ids = {str(r.id) for r in interaction.user.roles}
    return bool(user_role_ids & admin_role_ids)


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


def admin_check():
    """app_commands check decorator for admin-only commands."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Admin role to use this command.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


class Staff(commands.Cog, name="Staff"):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Shared log helper
    # ------------------------------------------------------------------
    async def _log(self, guild, title, fields, color=discord.Color.orange()):
        logger = self.bot.get_cog("Logging")
        if logger:
            await logger.log_raw(guild, title=title, fields=fields, color=color)

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

        await self._log(
            interaction.guild,
            title="🔧 Staff Override",
            fields=[
                ("Match ID", f"#{match_id}", True),
                ("Staff",    interaction.user.mention, True),
                ("Previous Status", old_status, True),
                ("Winner Set To", winner.mention, False),
                ("Reason", reason, False),
            ],
        )

        # Edit the dispute-channel message if one exists
        from views import _mark_dispute_handled
        await _mark_dispute_handled(
            self.bot,
            match_id,
            f"✅ **{interaction.user.display_name}** has resolved this case — "
            f"Match **#{match_id}** handled via `/override`.",
        )

        await interaction.followup.send(
            f"✅ Match **#{match_id}** has been overridden. Winner: {winner.mention}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="cancelm",
        description="[Staff] Cancel a match.",
    )
    @app_commands.describe(match_id="The Match ID to cancel (without #)")
    @staff_check()
    async def cancelm(
        self,
        interaction: discord.Interaction,
        match_id: str,
    ):
        match_id = match_id.upper().lstrip("#")
        match = await self.bot.db.get_match(match_id)
        if match is None:
            return await interaction.response.send_message(
                f"❌ Match **#{match_id}** not found.", ephemeral=True
            )
        if match["status"] in ("completed", "cancelled"):
            return await interaction.response.send_message(
                f"❌ Match **#{match_id}** is already {match['status']}.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.db.cancel_match(match_id, cancelled_by=str(interaction.user.id))

        match = await self.bot.db.get_match(match_id)
        matches_cog = self.bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(match)

        await self._log(
            interaction.guild,
            title="⚫ Match Cancelled",
            fields=[
                ("Match ID",     f"#{match_id}",           True),
                ("Cancelled By", interaction.user.mention, True),
            ],
            color=discord.Color.dark_gray(),
        )

        await interaction.followup.send(
            f"✅ Match **#{match_id}** has been cancelled.", ephemeral=True
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="forcepost",
        description="[Staff] Re-post the permanent challenge message.",
    )
    @staff_check()
    async def forcepost(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        challenges_cog = self.bot.get_cog("Challenges")
        if challenges_cog:
            await challenges_cog.ensure_challenge_message()
        await interaction.followup.send("✅ Challenge message re-posted.", ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="setelo",
        description="[Admin] Manually set a player's Elo rating.",
    )
    @app_commands.describe(user="The Discord member", elo="The new Elo value (0–3000)")
    @admin_check()
    async def set_elo(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        elo: int,
    ):
        if elo < 0 or elo > 3000:
            return await interaction.response.send_message(
                "❌ Elo must be between 0 and 3000.", ephemeral=True
            )
        await self.bot.db.reset_player_stats(str(user.id), elo=elo)

        await self._log(
            interaction.guild,
            title="🔧 Manual Elo Set",
            fields=[
                ("Staff",    interaction.user.mention, True),
                ("Player",   user.mention,             True),
                ("New Elo",  str(elo),                 True),
            ],
        )
        await interaction.response.send_message(
            f"✅ {user.mention}'s Elo set to **{elo}**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Reset commands
    # ------------------------------------------------------------------

    @app_commands.command(
        name="resetwins",
        description="[Staff] Reset a player's win count to 0.",
    )
    @app_commands.describe(user="The player whose wins you want to reset")
    @staff_check()
    async def reset_wins(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        success = await self.bot.db.reset_player_stats(str(user.id), wins=0)
        if not success:
            return await interaction.followup.send(
                "❌ Could not reset wins (player may not exist).", ephemeral=True
            )

        await self._log(
            interaction.guild,
            title="🔄 Wins Reset",
            fields=[
                ("Staff",  interaction.user.mention, True),
                ("Player", user.mention,             True),
                ("Wins reset to", "0",               True),
            ],
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            f"✅ Reset {user.mention}'s wins to **0**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="resetlosses",
        description="[Staff] Reset a player's loss count to 0.",
    )
    @app_commands.describe(user="The player whose losses you want to reset")
    @staff_check()
    async def reset_losses(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        success = await self.bot.db.reset_player_stats(str(user.id), losses=0)
        if not success:
            return await interaction.followup.send(
                "❌ Could not reset losses (player may not exist).", ephemeral=True
            )

        await self._log(
            interaction.guild,
            title="🔄 Losses Reset",
            fields=[
                ("Staff",  interaction.user.mention, True),
                ("Player", user.mention,             True),
                ("Losses reset to", "0",             True),
            ],
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            f"✅ Reset {user.mention}'s losses to **0**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="resetelo",
        description="[Staff] Reset a player's Elo rating to 1200.",
    )
    @app_commands.describe(user="The player whose Elo you want to reset")
    @staff_check()
    async def reset_elo(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        success = await self.bot.db.reset_player_stats(str(user.id), elo=1200)
        if not success:
            return await interaction.followup.send(
                "❌ Could not reset Elo (player may not exist).", ephemeral=True
            )

        await self._log(
            interaction.guild,
            title="🔄 Elo Reset",
            fields=[
                ("Staff",  interaction.user.mention, True),
                ("Player", user.mention,             True),
                ("Elo reset to", "1200",             True),
            ],
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            f"✅ Reset {user.mention}'s Elo to **1200**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    @app_commands.command(
        name="resetall",
        description="[Staff] Reset a player's wins, losses, Elo, and forfeit count.",
    )
    @app_commands.describe(user="The player to fully reset")
    @staff_check()
    async def reset_all(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        success = await self.bot.db.reset_player_stats(
            str(user.id), wins=0, losses=0, elo=1200, forfeit_count=0
        )
        if not success:
            return await interaction.followup.send(
                "❌ Could not reset stats (player may not exist).", ephemeral=True
            )

        await self._log(
            interaction.guild,
            title="🔄 Full Stats Reset",
            fields=[
                ("Staff",          interaction.user.mention,           True),
                ("Player",         user.mention,                       True),
                ("Reset",          "Wins: 0 | Losses: 0 | Elo: 1200 | Forfeits: 0", False),
            ],
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            f"✅ All stats reset for {user.mention} — Wins: **0**, Losses: **0**, "
            f"Elo: **1200**, Forfeits: **0**.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Staff(bot))
