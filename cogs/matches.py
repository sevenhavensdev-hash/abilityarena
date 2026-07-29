"""
cogs/matches.py

Handles:
 - Updating forum post embeds when match status changes.
 - Posting confirm / dispute / forfeit notices (dispute/forfeit go to DISPUTE_CHANNEL_ID).
 - Finalising matches (Elo calculation, new completion message, thread archive).
 - Auto-forfeit background task (3-day stale match check, hourly).
 - /match and /mymatches slash commands.

Environment variables used:
  DISPUTE_CHANNEL_ID  — channel where dispute and forfeit cases are sent for staff review.
                        Falls back to posting in the forum thread if not set.
"""

from __future__ import annotations

import os
import logging
import discord
from discord import app_commands
from discord.ext import commands, tasks

from views import (
    ConfirmResultView,
    ReportResultView,
    StaffOverrideView,
    StaffForfeitApprovalView,
    STATUS_LABELS,
)

log = logging.getLogger("cogs.matches")

STATUS_COLORS = {
    "awaiting":          discord.Color.yellow(),
    "in_progress":       discord.Color.blue(),
    "awaiting_confirm":  discord.Color.orange(),
    "awaiting_forfeit":  discord.Color.purple(),
    "completed":         discord.Color.green(),
    "disputed":          discord.Color.red(),
    "cancelled":         discord.Color.dark_gray(),
}


def _match_embed(match) -> discord.Embed:
    """Build the canonical match embed. Always includes a 'Match ID' field
    so the dispute-channel embed lookup in _match_from_thread works."""
    status       = match["status"]
    status_label = STATUS_LABELS.get(status, status)
    color        = STATUS_COLORS.get(status, discord.Color.default())
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


async def _get_dispute_channel(bot) -> discord.TextChannel | None:
    """Fetch the configured DISPUTE_CHANNEL_ID channel, or None if not set."""
    channel_id = os.getenv("DISPUTE_CHANNEL_ID")
    if not channel_id:
        return None
    ch = bot.get_channel(int(channel_id))
    if ch is None:
        try:
            ch = await bot.fetch_channel(int(channel_id))
        except Exception as exc:
            log.warning("Could not fetch DISPUTE_CHANNEL_ID %s: %s", channel_id, exc)
            return None
    return ch


class Matches(commands.Cog, name="Matches"):
    def __init__(self, bot):
        self.bot = bot
        self.auto_forfeit_task.start()

    def cog_unload(self):
        self.auto_forfeit_task.cancel()

    # ------------------------------------------------------------------
    # Background task — auto-forfeit stale matches after 3 days
    # ------------------------------------------------------------------
    @tasks.loop(hours=1)
    async def auto_forfeit_task(self):
        """
        Runs every hour. Finds matches that have been stuck in 'awaiting' for
        more than 3 days and haven't been notified yet. Auto-flags the opponent
        for forfeit and sends a review notice to the dispute channel (or forum
        thread as fallback).
        """
        try:
            stale = await self.bot.db.get_stale_matches(seconds=259200)
            for match in stale:
                # Prevent re-processing
                await self.bot.db.mark_forfeit_notified(match["match_id"])
                # Flag the opponent (who hasn't responded)
                await self.bot.db.request_forfeit(match["match_id"], match["opponent_id"])
                match = await self.bot.db.get_match(match["match_id"])

                # Resolve the guild from the forum thread
                guild = None
                if match["forum_thread_id"]:
                    try:
                        thread = self.bot.get_channel(int(match["forum_thread_id"]))
                        if thread is None:
                            thread = await self.bot.fetch_channel(int(match["forum_thread_id"]))
                        guild = thread.guild
                    except Exception:
                        pass

                if guild is None and self.bot.guilds:
                    guild = self.bot.guilds[0]

                if guild:
                    await self.post_forfeit_notice(match, guild, forfeiter=None, auto=True)
                    log.info("Auto-forfeit flagged match %s.", match["match_id"])

        except Exception as exc:
            log.exception("Error in auto_forfeit_task: %s", exc)

    @auto_forfeit_task.before_loop
    async def before_auto_forfeit(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _get_forum_message(self, match) -> discord.Message | None:
        if not match["forum_thread_id"] or not match["forum_message_id"]:
            return None
        try:
            thread = self.bot.get_channel(int(match["forum_thread_id"]))
            if thread is None:
                thread = await self.bot.fetch_channel(int(match["forum_thread_id"]))
            return await thread.fetch_message(int(match["forum_message_id"]))
        except Exception as exc:
            log.warning("Could not fetch forum message: %s", exc)
            return None

    async def _get_forum_thread(self, match):
        if not match["forum_thread_id"]:
            return None
        try:
            thread = self.bot.get_channel(int(match["forum_thread_id"]))
            if thread is None:
                thread = await self.bot.fetch_channel(int(match["forum_thread_id"]))
            return thread
        except Exception as exc:
            log.warning("Could not fetch forum thread: %s", exc)
            return None

    # ------------------------------------------------------------------
    async def update_forum_post(self, match):
        """
        Re-renders the forum post embed. For disputed and awaiting_forfeit
        matches the action buttons are now in the dispute channel, so the
        forum post shows the updated embed with no interactive buttons.
        """
        msg = await self._get_forum_message(match)
        if msg is None:
            return

        embed  = _match_embed(match)
        status = match["status"]

        if status in ("awaiting", "in_progress", "awaiting_confirm"):
            view = ReportResultView()
        else:
            # disputed, awaiting_forfeit, completed, cancelled — no buttons here;
            # staff handles disputed/awaiting_forfeit via the dispute channel.
            view = discord.ui.View()

        try:
            await msg.edit(embed=embed, view=view)
        except Exception as exc:
            log.warning("Could not update forum message: %s", exc)

    # ------------------------------------------------------------------
    async def post_confirm_request(self, match, guild: discord.Guild):
        """Posts a confirm/dispute message in the forum thread after result is reported."""
        thread = await self._get_forum_thread(match)
        if thread is None:
            return
        try:
            other_id = (
                match["opponent_id"]
                if match["reporter_id"] == match["challenger_id"]
                else match["challenger_id"]
            )
            embed = discord.Embed(
                title="🏆 Match Result Reported",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Match ID",       value=f"#{match['match_id']}", inline=False)
            embed.add_field(
                name="Reported Winner",
                value=f"<@{match['winner_id']}> (**{match['winner_roblox']}**)",
                inline=True,
            )
            embed.add_field(
                name="Reported Loser",
                value=f"<@{match['loser_id']}> (**{match['loser_roblox']}**)",
                inline=True,
            )
            await thread.send(
                content=f"<@{other_id}>, please confirm or dispute this result.",
                embed=embed,
                view=ConfirmResultView(),
            )
        except Exception as exc:
            log.warning("Could not post confirm request: %s", exc)

    # ------------------------------------------------------------------
    async def post_dispute_notice(
        self, match, guild: discord.Guild, disputer: discord.Member | None = None
    ):
        """
        Posts a dispute notice to DISPUTE_CHANNEL_ID with a StaffOverrideView.
        Falls back to the forum thread if the channel isn't configured.
        Stores the posted message ID in the database so it can be edited later.
        """
        staff_role_id = os.getenv("STAFF_ROLE_ID")
        staff_mention = f"<@&{staff_role_id}>" if staff_role_id else "Staff"

        embed = _match_embed(match)
        embed.title = "🔴 Match Disputed — Staff Review Required"

        if disputer:
            embed.add_field(name="Disputed By", value=disputer.mention, inline=True)

        content = (
            f"🔴 {staff_mention} — Match **#{match['match_id']}** has been disputed twice "
            f"and requires a staff override. Please review the details above and use the "
            f"**Staff Override** button to resolve."
        )

        dispute_ch = await _get_dispute_channel(self.bot)

        if dispute_ch is not None:
            # Post to the dedicated dispute channel
            try:
                msg = await dispute_ch.send(
                    content=content,
                    embed=embed,
                    view=StaffOverrideView(),
                )
                await self.bot.db.store_dispute_message(
                    match["match_id"], str(msg.id), str(dispute_ch.id)
                )
                log.info(
                    "Dispute notice for match %s posted to #%s.",
                    match["match_id"], dispute_ch.name,
                )
            except Exception as exc:
                log.warning("Could not post dispute notice to dispute channel: %s", exc)
        else:
            # Fallback: post in the forum thread with the staff button
            log.warning(
                "DISPUTE_CHANNEL_ID not set — posting StaffOverrideView in forum thread for match %s.",
                match["match_id"],
            )
            thread = await self._get_forum_thread(match)
            if thread:
                try:
                    await thread.send(
                        content=content,
                        embed=embed,
                        view=StaffOverrideView(),
                    )
                except Exception as exc:
                    log.warning("Fallback dispute notice failed: %s", exc)

    # ------------------------------------------------------------------
    async def post_forfeit_notice(
        self,
        match,
        guild: discord.Guild,
        forfeiter: discord.Member | None = None,
        auto: bool = False,
    ):
        """
        Posts a forfeit notice to DISPUTE_CHANNEL_ID with a StaffForfeitApprovalView.
        Falls back to the forum thread if the channel isn't configured.
        Also sends a brief notification in the forum thread (no action buttons).
        Stores the posted message ID in the database.
        """
        staff_role_id = os.getenv("STAFF_ROLE_ID")
        staff_mention = f"<@&{staff_role_id}>" if staff_role_id else "Staff"

        forfeit_count = await self.bot.db.get_player_forfeit_count(match["opponent_id"])
        history_note  = (
            f"\n⚠️ Note: <@{match['opponent_id']}> has forfeited **{forfeit_count}** time(s) before."
            if forfeit_count >= 1 else ""
        )

        embed = _match_embed(match)

        if auto:
            embed.title   = "⏰ Auto-Forfeit — No Response (3 Days)"
            trigger_text  = (
                f"<@{match['opponent_id']}> has not responded to this match for over **3 days** "
                f"and has been automatically flagged for forfeit."
            )
        else:
            forfeiter_mention = forfeiter.mention if forfeiter else f"<@{match['forfeiter_id']}>"
            embed.title       = "🏳️ Forfeit Requested — Staff Review Required"
            trigger_text      = f"{forfeiter_mention} has requested to forfeit this match."

        content = (
            f"🏳️ {staff_mention} — {trigger_text} "
            f"Match **#{match['match_id']}** needs staff approval.{history_note}"
        )

        dispute_ch = await _get_dispute_channel(self.bot)

        if dispute_ch is not None:
            try:
                msg = await dispute_ch.send(
                    content=content,
                    embed=embed,
                    view=StaffForfeitApprovalView(),
                )
                await self.bot.db.store_dispute_message(
                    match["match_id"], str(msg.id), str(dispute_ch.id)
                )
                log.info(
                    "Forfeit notice for match %s posted to #%s.",
                    match["match_id"], dispute_ch.name,
                )

                # Brief notification in forum thread (no action buttons)
                thread = await self._get_forum_thread(match)
                if thread:
                    try:
                        await thread.send(
                            "🏳️ A forfeit request has been submitted. "
                            "Staff are reviewing this case in the dispute channel."
                        )
                    except Exception:
                        pass

            except Exception as exc:
                log.warning("Could not post forfeit notice to dispute channel: %s", exc)
        else:
            # Fallback: post in forum thread
            log.warning(
                "DISPUTE_CHANNEL_ID not set — posting StaffForfeitApprovalView in forum thread for match %s.",
                match["match_id"],
            )
            thread = await self._get_forum_thread(match)
            if thread:
                try:
                    await thread.send(
                        content=content,
                        embed=embed,
                        view=StaffForfeitApprovalView(),
                    )
                except Exception as exc:
                    log.warning("Fallback forfeit notice failed: %s", exc)

    # ------------------------------------------------------------------
    async def finalise_match(self, match, guild: discord.Guild, how: str):
        """
        Calculates Elo, updates DB, removes buttons from original forum post,
        sends a NEW completion message in the forum thread, and archives it.
        `how` is one of: 'opponent_confirm', 'staff_override', 'forfeit'
        """
        elo_cog = self.bot.get_cog("Elo")
        if elo_cog is None:
            log.error("Elo cog not loaded — cannot finalise match.")
            return

        winner_id     = match["winner_id"]
        loser_id      = match["loser_id"]
        challenger_id = match["challenger_id"]

        winner_player = await self.bot.db.get_or_create_player(winner_id)
        loser_player  = await self.bot.db.get_or_create_player(loser_id)

        winner_new, loser_new = elo_cog.calculate(
            winner_player["elo"], loser_player["elo"]
        )

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

        await self.bot.db.update_player_elo(winner_id, winner_new, won=True)
        await self.bot.db.update_player_elo(loser_id,  loser_new,  won=False)

        match = await self.bot.db.get_match(match["match_id"])

        # 1. Remove buttons from the original forum post (keep its embed as-is)
        msg = await self._get_forum_message(match)
        if msg:
            try:
                await msg.edit(view=discord.ui.View())
            except Exception as exc:
                log.warning("Could not remove buttons from original forum post: %s", exc)

        # 2. Send a NEW completion message in the forum thread
        thread = await self._get_forum_thread(match)
        if thread:
            try:
                how_label = how.replace("_", " ").title()
                embed     = _match_embed(match)
                await thread.send(
                    content=f"🏆 **Match Complete** ({how_label})",
                    embed=embed,
                )
            except Exception as exc:
                log.warning("Could not post completion message: %s", exc)

            # 3. Archive and lock the thread
            try:
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
            won    = m["winner_id"] == str(interaction.user.id)
            result = "🏆 Won" if won else "❌ Lost"
            opp_id = m["opponent_id"] if m["challenger_id"] == str(interaction.user.id) else m["challenger_id"]
            embed.add_field(
                name=f"#{m['match_id']} {result}",
                value=f"vs <@{opp_id}> | Region: {m['region']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Matches(bot))
