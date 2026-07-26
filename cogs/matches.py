"""
cogs/matches.py

Handles:
 - Updating forum post embeds when match status changes.
 - Posting confirm/dispute request messages.
 - Finalising matches (Elo calculation, archiving thread).
 - /match command (lookup by ID).
"""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

from views import (
    ConfirmResultView,
    ReportResultView,
    StaffOverrideView,
    STATUS_LABELS,
)

log = logging.getLogger("cogs.matches")

STATUS_COLORS = {
    "awaiting":         discord.Color.yellow(),
    "in_progress":      discord.Color.blue(),
    "awaiting_confirm": discord.Color.orange(),
    "completed":        discord.Color.green(),
    "disputed":         discord.Color.red(),
    "cancelled":        discord.Color.dark_gray(),
}


def _match_embed(match) -> discord.Embed:
    status = match["status"]
    status_label = STATUS_LABELS.get(status, status)
    color = STATUS_COLORS.get(status, discord.Color.default())
    embed = discord.Embed(title="⚔️ Competitive Duel", color=color)
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
    embed.add_field(name="Status", value=status_label, inline=False)

    if status == "completed" and match["winner_id"]:
        c_before = match["challenger_elo_before"] or "?"
        c_after  = match["challenger_elo_after"]  or "?"
        o_before = match["opponent_elo_before"]   or "?"
        o_after  = match["opponent_elo_after"]    or "?"
        c_diff   = (match["challenger_elo_after"] - match["challenger_elo_before"]) if match["challenger_elo_after"] else 0
        o_diff   = (match["opponent_elo_after"]   - match["opponent_elo_before"])   if match["opponent_elo_after"]  else 0
        embed.add_field(
            name="🏆 Winner",
            value=f"<@{match['winner_id']}> (**{match['winner_roblox']}**)",
            inline=True,
        )
        embed.add_field(
            name="Elo Changes",
            value=(
                f"<@{match['challenger_id']}>: {c_before} → {c_after} "
                f"({'+'if c_diff >= 0 else ''}{c_diff})\n"
                f"<@{match['opponent_id']}>:  {o_before} → {o_after} "
                f"({'+'if o_diff >= 0 else ''}{o_diff})"
            ),
            inline=False,
        )
    return embed


class Matches(commands.Cog, name="Matches"):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------
    async def _get_forum_message(self, match) -> discord.Message | None:
        if not match["forum_thread_id"] or not match["forum_message_id"]:
            return None
        try:
            thread = self.bot.get_channel(int(match["forum_thread_id"]))
            if thread is None:
                thread = await self.bot.fetch_channel(int(match["forum_thread_id"]))
            msg = await thread.fetch_message(int(match["forum_message_id"]))
            return msg
        except Exception as exc:
            log.warning("Could not fetch forum message: %s", exc)
            return None

    # ------------------------------------------------------------------
    async def update_forum_post(self, match):
        """Re-renders the embed in the forum post to reflect current match status."""
        msg = await self._get_forum_message(match)
        if msg is None:
            return

        embed = _match_embed(match)
        status = match["status"]

        # Pick correct action view
        if status in ("awaiting", "in_progress"):
            view = ReportResultView()
        elif status == "awaiting_confirm":
            # Still show Report + Cancel but they will be blocked by status checks
            view = ReportResultView()
        elif status == "disputed":
            view = StaffOverrideView()
        else:
            view = discord.ui.View()  # no buttons when completed/cancelled

        try:
            await msg.edit(embed=embed, view=view)
        except Exception as exc:
            log.warning("Could not update forum message: %s", exc)

    # ------------------------------------------------------------------
    async def post_confirm_request(self, match, guild: discord.Guild):
        """Posts a confirm/dispute message in the thread."""
        if not match["forum_thread_id"]:
            return
        try:
            thread = self.bot.get_channel(int(match["forum_thread_id"]))
            if thread is None:
                thread = await self.bot.fetch_channel(int(match["forum_thread_id"]))

            other_id = (
                match["opponent_id"]
                if match["reporter_id"] == match["challenger_id"]
                else match["challenger_id"]
            )
            embed = discord.Embed(
                title="🏆 Match Result Reported",
                color=discord.Color.orange(),
            )
            embed.add_field(
                name="Reported Winner",
                value=f"<@{match['winner_id']}> (**{match['winner_roblox']}**)",
                inline=False,
            )
            embed.add_field(
                name="Reported Loser",
                value=f"<@{match['loser_id']}> (**{match['loser_roblox']}**)",
                inline=False,
            )
            embed.set_footer(text=f"<@{other_id}>, please confirm or dispute this result.")

            await thread.send(
                content=f"<@{other_id}>, please confirm or dispute this result.",
                embed=embed,
                view=ConfirmResultView(),
            )
        except Exception as exc:
            log.warning("Could not post confirm request: %s", exc)

    # ------------------------------------------------------------------
    async def finalise_match(self, match, guild: discord.Guild, how: str):
        """
        Calculates Elo, updates DB, updates the forum post, and archives the thread.
        `how` is one of: 'opponent_confirm', 'staff_override'
        """
        elo_cog = self.bot.get_cog("Elo")
        if elo_cog is None:
            log.error("Elo cog not loaded — cannot finalise match.")
            return

        winner_id   = match["winner_id"]
        loser_id    = match["loser_id"]
        challenger_id = match["challenger_id"]
        opponent_id   = match["opponent_id"]

        winner_player  = await self.bot.db.get_or_create_player(winner_id)
        loser_player   = await self.bot.db.get_or_create_player(loser_id)

        # Elo calculation
        winner_new, loser_new = elo_cog.calculate(
            winner_player["elo"], loser_player["elo"]
        )

        # Map back to challenger/opponent
        if winner_id == challenger_id:
            c_before, c_after = winner_player["elo"], winner_new
            o_before, o_after = loser_player["elo"],  loser_new
        else:
            c_before, c_after = loser_player["elo"],  loser_new
            o_before, o_after = winner_player["elo"], winner_new

        persisted = await self.bot.db.apply_elo(
            match["match_id"],
            challenger_elo_before=c_before,
            opponent_elo_before=o_before,
            challenger_elo_after=c_after,
            opponent_elo_after=o_after,
        )
        if not persisted:
            log.warning("Elo already applied for match %s — skipping.", match["match_id"])
            return

        # Update individual player rows
        await self.bot.db.update_player_elo(winner_id, winner_new, won=True)
        await self.bot.db.update_player_elo(loser_id,  loser_new,  won=False)

        match = await self.bot.db.get_match(match["match_id"])

        # Update the forum embed
        await self.update_forum_post(match)

        # Archive / lock the thread
        if match["forum_thread_id"]:
            try:
                thread = self.bot.get_channel(int(match["forum_thread_id"]))
                if thread is None:
                    thread = await self.bot.fetch_channel(int(match["forum_thread_id"]))
                await thread.edit(archived=True, locked=True)
            except Exception as exc:
                log.warning("Could not archive forum thread: %s", exc)

        # Log completion
        logger = self.bot.get_cog("Logging")
        if logger:
            await logger.log_match_completed(guild, match, how)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    @app_commands.command(name="match", description="Look up a match by its ID.")
    @app_commands.describe(match_id="The match ID (without the # symbol)")
    async def match_lookup(self, interaction: discord.Interaction, match_id: str):
        match = await self.bot.db.get_match(match_id.upper().lstrip("#"))
        if match is None:
            return await interaction.response.send_message(
                f"❌ Match **#{match_id}** not found.", ephemeral=True
            )
        embed = _match_embed(match)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="mymatches", description="View your recent match history.")
    async def my_matches(self, interaction: discord.Interaction):
        rows = await self.bot.db.get_player_matches(str(interaction.user.id), limit=5)
        if not rows:
            return await interaction.response.send_message(
                "You have no completed matches yet.", ephemeral=True
            )
        embed = discord.Embed(
            title=f"📋 Match History — {interaction.user.display_name}",
            color=discord.Color.blurple(),
        )
        for m in rows:
            won = m["winner_id"] == str(interaction.user.id)
            result_str = "🏆 Won" if won else "❌ Lost"
            opp_id = m["opponent_id"] if m["challenger_id"] == str(interaction.user.id) else m["challenger_id"]
            embed.add_field(
                name=f"#{m['match_id']} {result_str}",
                value=f"vs <@{opp_id}> | Region: {m['region']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Matches(bot))
