"""
views.py — All persistent Discord UI Views, Buttons, Selects, and Modals.

Views that need to survive bot restarts must:
  1. Have a stable custom_id on every Item.
  2. Be registered with bot.add_view() in setup_hook (bot.py).

Views fetch the Database via interaction.client.db.
Logging is dispatched via the LoggingCog helper.
"""

from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot import DuelBot

log = logging.getLogger("views")

REGIONS = ["Asia", "North America", "South America", "Europe", "Oceania", "Middle East", "Africa"]

STATUS_LABELS = {
    "awaiting":         "🟡 Awaiting Match",
    "in_progress":      "🔵 Match in Progress",
    "awaiting_confirm": "🟠 Awaiting Result Confirmation",
    "completed":        "🟢 Completed",
    "disputed":         "🔴 Disputed",
    "cancelled":        "⚫ Cancelled",
}


def _logger(interaction: discord.Interaction):
    """Shortcut to the LoggingCog helper."""
    return interaction.client.get_cog("Logging")


# ============================================================
# MODALS
# ============================================================

class ChallengeModal(discord.ui.Modal, title="⚔️ Create a Duel Challenge"):
    """Shown after the user picks a region from the dropdown."""

    challenger_roblox = discord.ui.TextInput(
        label="Your Roblox Username",
        placeholder="e.g. Leon_Roblox",
        max_length=50,
    )
    opponent_roblox = discord.ui.TextInput(
        label="Opponent's Roblox Username",
        placeholder="e.g. John_Roblox",
        max_length=50,
    )
    opponent_discord = discord.ui.TextInput(
        label="Opponent's Discord User ID",
        placeholder="Right-click their name → Copy User ID",
        max_length=30,
    )

    def __init__(self, region: str):
        super().__init__()
        self.region = region

    async def on_submit(self, interaction: discord.Interaction):
        bot: DuelBot = interaction.client
        guild = interaction.guild

        challenger_roblox = self.challenger_roblox.value.strip()
        opponent_roblox   = self.opponent_roblox.value.strip()
        matched_region    = self.region  # already validated by the select
        opponent_id_raw   = self.opponent_discord.value.strip()

        # ── Validate opponent Discord ID ─────────────────────────
        try:
            opponent_id = int(opponent_id_raw)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid Discord User ID. Right-click the user's name and select **Copy User ID**.",
                ephemeral=True,
            )

        opponent_member = guild.get_member(opponent_id)
        if opponent_member is None:
            return await interaction.response.send_message(
                "❌ That user is not a member of this server.",
                ephemeral=True,
            )
        if opponent_member.bot:
            return await interaction.response.send_message(
                "❌ You cannot challenge a bot.",
                ephemeral=True,
            )
        if opponent_member.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You cannot challenge yourself.",
                ephemeral=True,
            )

        # ── Duplicate match check ─────────────────────────────────
        has_active = await bot.db.has_active_match(
            str(interaction.user.id), str(opponent_id)
        )
        if has_active:
            return await interaction.response.send_message(
                "❌ You already have an active match against this opponent. "
                "Resolve it before creating a new challenge.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        # ── Create match ──────────────────────────────────────────
        challenges_cog = bot.get_cog("Challenges")
        if challenges_cog is None:
            return await interaction.followup.send(
                "❌ Internal error: Challenges cog not loaded.", ephemeral=True
            )

        match = await challenges_cog.create_challenge(
            interaction=interaction,
            challenger_roblox=challenger_roblox,
            opponent=opponent_member,
            opponent_roblox=opponent_roblox,
            region=matched_region,
        )
        if match is None:
            return await interaction.followup.send(
                "❌ Failed to create challenge. Please try again.", ephemeral=True
            )

        thread_link = f"\n📌 View your match: <#{match['forum_thread_id']}>" if match["forum_thread_id"] else ""
        await interaction.followup.send(
            f"✅ Challenge created! Match ID: **#{match['match_id']}**\n"
            f"A forum post has been created and {opponent_member.mention} has been notified."
            f"{thread_link}",
            ephemeral=True,
        )


class ReportResultModal(discord.ui.Modal, title="🏆 Report Match Result"):
    """Shown when a player clicks Report Match Result."""

    winner_choice = discord.ui.TextInput(
        label="Who won? Enter 'me' or 'opponent'",
        placeholder="Type 'me' if you won, 'opponent' if they won",
        max_length=10,
    )

    def __init__(self, match_id: str):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        bot: DuelBot = interaction.client
        match = await bot.db.get_match(self.match_id)
        if match is None:
            return await interaction.response.send_message(
                "❌ Match not found.", ephemeral=True
            )

        user_id = str(interaction.user.id)
        choice = self.winner_choice.value.strip().lower()

        if user_id not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ You are not a participant in this match.", ephemeral=True
            )
        if match["status"] not in ("awaiting", "in_progress"):
            return await interaction.response.send_message(
                f"❌ This match cannot accept a result report. Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )
        if match["reporter_id"] is not None:
            return await interaction.response.send_message(
                "❌ A result has already been reported for this match.", ephemeral=True
            )

        if choice == "me":
            winner_id = user_id
        elif choice in ("opponent", "them"):
            if user_id == match["challenger_id"]:
                winner_id = match["opponent_id"]
            else:
                winner_id = match["challenger_id"]
        else:
            return await interaction.response.send_message(
                "❌ Please type **me** or **opponent**.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        success = await bot.db.report_result(
            self.match_id, reporter_id=user_id, winner_id=winner_id
        )
        if not success:
            return await interaction.followup.send(
                "❌ Could not record result (already reported).", ephemeral=True
            )

        match = await bot.db.get_match(self.match_id)

        # Update forum post
        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(match)
            await matches_cog.post_confirm_request(match, interaction.guild)

        logger = _logger(interaction)
        if logger:
            await logger.log_result_reported(interaction.guild, match, interaction.user)

        await interaction.followup.send(
            "✅ Result reported. Waiting for opponent confirmation.", ephemeral=True
        )


class StaffOverrideModal(discord.ui.Modal, title="🔧 Staff Override"):
    winner_id_input = discord.ui.TextInput(
        label="Winner's Discord User ID",
        placeholder="Enter the Discord User ID of the winner",
        max_length=30,
    )
    reason = discord.ui.TextInput(
        label="Reason for Override",
        placeholder="Brief explanation of why this override is necessary",
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, match_id: str):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        bot: DuelBot = interaction.client

        # Permission check
        staff_role_id = os.getenv("STAFF_ROLE_ID")
        if not staff_role_id:
            return await interaction.response.send_message(
                "❌ STAFF_ROLE_ID is not configured.", ephemeral=True
            )
        staff_role = interaction.guild.get_role(int(staff_role_id))
        if staff_role not in interaction.user.roles:
            return await interaction.response.send_message(
                "❌ You do not have permission to override match results.", ephemeral=True
            )

        try:
            winner_id = str(int(self.winner_id_input.value.strip()))
        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid Discord User ID.", ephemeral=True
            )

        match = await bot.db.get_match(self.match_id)
        if match is None:
            return await interaction.response.send_message("❌ Match not found.", ephemeral=True)

        if winner_id not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ The winner must be one of the two match participants.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        old_status = match["status"]
        success = await bot.db.staff_override(
            self.match_id,
            staff_id=str(interaction.user.id),
            winner_id=winner_id,
            reason=self.reason.value.strip(),
        )
        if not success:
            return await interaction.followup.send("❌ Override failed.", ephemeral=True)

        match = await bot.db.get_match(self.match_id)

        # Apply Elo
        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.finalise_match(match, interaction.guild, how="staff_override")

        logger = _logger(interaction)
        if logger:
            await logger.log_staff_override(
                interaction.guild, match, interaction.user, old_status
            )

        await interaction.followup.send(
            f"✅ Override applied. Match **#{self.match_id}** resolved.", ephemeral=True
        )


# ============================================================
# PERSISTENT VIEWS
# ============================================================

class RegionSelectView(discord.ui.View):
    """Ephemeral region picker shown before the challenge modal."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Choose your region…",
        custom_id="region_select_dropdown",
        options=[discord.SelectOption(label=r, value=r) for r in REGIONS],
    )
    async def region_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        region = select.values[0]
        await interaction.response.send_modal(ChallengeModal(region=region))
        self.stop()


class CreateChallengeView(discord.ui.View):
    """Permanent view on the challenge message. Survives restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="⚔️ Create Challenge",
        style=discord.ButtonStyle.primary,
        custom_id="create_challenge_btn",
    )
    async def create_challenge(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "📍 Please select your region to continue:",
            view=RegionSelectView(),
            ephemeral=True,
        )


# ------------------------------------------------------------------

class ReportResultView(discord.ui.View):
    """Attached to each forum post. Report / Cancel buttons."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🏆 Report Match Result",
        style=discord.ButtonStyle.success,
        custom_id="report_result_btn",
    )
    async def report_result(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        bot: DuelBot = interaction.client
        # Match ID is encoded in the thread topic or fetched by thread id
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match for this thread.", ephemeral=True
            )
        if str(interaction.user.id) not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ Only match participants can report results.", ephemeral=True
            )
        if match["status"] not in ("awaiting", "in_progress"):
            return await interaction.response.send_message(
                f"❌ Cannot report result. Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )
        if match["reporter_id"] is not None:
            return await interaction.response.send_message(
                "❌ A result has already been reported.", ephemeral=True
            )
        await interaction.response.send_modal(ReportResultModal(match["match_id"]))

    @discord.ui.button(
        label="❌ Cancel Match",
        style=discord.ButtonStyle.danger,
        custom_id="cancel_match_btn",
    )
    async def cancel_match(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        user_id = str(interaction.user.id)
        is_participant = user_id in (match["challenger_id"], match["opponent_id"])
        is_staff = _is_staff(interaction)
        if not is_participant and not is_staff:
            return await interaction.response.send_message(
                "❌ Only participants or staff can cancel this match.", ephemeral=True
            )
        if match["status"] in ("completed", "cancelled"):
            return await interaction.response.send_message(
                f"❌ Match is already {STATUS_LABELS.get(match['status'], match['status'])}.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.db.cancel_match(match["match_id"], cancelled_by=user_id)
        match = await bot.db.get_match(match["match_id"])

        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(match)

        logger = _logger(interaction)
        if logger:
            await logger.log_match_cancelled(interaction.guild, match, interaction.user)

        await interaction.followup.send("✅ Match cancelled.", ephemeral=True)


class ConfirmResultView(discord.ui.View):
    """Confirm / Dispute buttons sent after result is reported."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Confirm Result",
        style=discord.ButtonStyle.success,
        custom_id="confirm_result_btn",
    )
    async def confirm_result(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        user_id = str(interaction.user.id)
        if user_id not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ Only match participants can confirm results.", ephemeral=True
            )
        if user_id == match["reporter_id"]:
            return await interaction.response.send_message(
                "❌ You cannot confirm your own result report.", ephemeral=True
            )
        if match["status"] != "awaiting_confirm":
            return await interaction.response.send_message(
                f"❌ No pending result to confirm. Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        success = await bot.db.confirm_result(match["match_id"], confirmer_id=user_id)
        if not success:
            return await interaction.followup.send(
                "❌ Could not confirm (already confirmed or you reported it).", ephemeral=True
            )

        match = await bot.db.get_match(match["match_id"])
        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.finalise_match(match, interaction.guild, how="opponent_confirm")

        logger = _logger(interaction)
        if logger:
            await logger.log_result_confirmed(interaction.guild, match, interaction.user)

        # Update the confirm/dispute message to show who confirmed and remove buttons
        try:
            confirmed_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if confirmed_embed:
                confirmed_embed.colour = discord.Color.green()
                confirmed_embed.set_footer(text=f"✅ Confirmed by {interaction.user.display_name}")
            await interaction.message.edit(
                content=f"✅ {interaction.user.mention} confirmed the result. Elo has been updated!",
                embed=confirmed_embed,
                view=discord.ui.View(),  # removes all buttons
            )
        except Exception:
            pass

        await interaction.followup.send("✅ Result confirmed. Elo has been updated.", ephemeral=True)

    @discord.ui.button(
        label="❌ Dispute Result",
        style=discord.ButtonStyle.danger,
        custom_id="dispute_result_btn",
    )
    async def dispute_result(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        user_id = str(interaction.user.id)
        if user_id not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ Only match participants can dispute results.", ephemeral=True
            )
        if user_id == match["reporter_id"]:
            return await interaction.response.send_message(
                "❌ You cannot dispute your own result report. Only the other player can dispute it.",
                ephemeral=True,
            )
        if match["status"] != "awaiting_confirm":
            return await interaction.response.send_message(
                f"❌ Nothing to dispute. Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        dispute_count = await bot.db.dispute_result(match["match_id"], disputer_id=user_id)
        match = await bot.db.get_match(match["match_id"])

        if dispute_count == 1:
            # First dispute — warn and keep buttons active
            try:
                await interaction.message.edit(
                    content=(
                        f"⚠️ {interaction.user.mention} disputed the result once.\n"
                        f"**If you dispute again, staff will be called in to review.**\n"
                        f"The other player can still confirm if this was a mistake."
                    ),
                    view=ConfirmResultView(),
                )
            except Exception:
                pass
            await interaction.followup.send(
                "⚠️ First dispute recorded. Dispute once more to escalate to staff, "
                "or the other player can still confirm the result.",
                ephemeral=True,
            )
        else:
            # Second dispute — finalize and ping staff
            matches_cog = bot.get_cog("Matches")
            if matches_cog:
                await matches_cog.update_forum_post(match)

            staff_role_id = os.getenv("STAFF_ROLE_ID")
            staff_mention = f"<@&{staff_role_id}>" if staff_role_id else "Staff"
            try:
                thread_id = match["forum_thread_id"]
                if thread_id:
                    thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                    await thread.send(
                        f"{staff_mention} — Match **#{match['match_id']}** has been disputed twice. "
                        f"A staff override is required."
                    )
            except Exception:
                pass

            try:
                await interaction.message.edit(
                    content=(
                        f"🔴 {interaction.user.mention} has disputed this result a second time. "
                        f"Staff have been notified."
                    ),
                    view=discord.ui.View(),  # remove buttons
                )
            except Exception:
                pass

            logger = _logger(interaction)
            if logger:
                await logger.log_result_disputed(interaction.guild, match, interaction.user)

            await interaction.followup.send(
                "🔴 Result disputed twice. Staff have been pinged to review this match.",
                ephemeral=True,
            )


class StaffOverrideView(discord.ui.View):
    """Staff-only override button, shown on disputed matches."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔧 Staff Override",
        style=discord.ButtonStyle.secondary,
        custom_id="staff_override_btn",
    )
    async def staff_override(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not _is_staff(interaction):
            return await interaction.response.send_message(
                "❌ You do not have the Staff role required for this action.", ephemeral=True
            )
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        await interaction.response.send_modal(StaffOverrideModal(match["match_id"]))


class LeaderboardView(discord.ui.View):
    """Pagination buttons for the leaderboard."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="◀ Previous",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_prev",
    )
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        lb_cog = interaction.client.get_cog("Leaderboard")
        if lb_cog:
            await lb_cog.paginate(interaction, direction=-1)

    @discord.ui.button(
        label="Next ▶",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_next",
    )
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        lb_cog = interaction.client.get_cog("Leaderboard")
        if lb_cog:
            await lb_cog.paginate(interaction, direction=1)

    @discord.ui.button(
        label="🔄 Refresh",
        style=discord.ButtonStyle.primary,
        custom_id="leaderboard_refresh",
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        lb_cog = interaction.client.get_cog("Leaderboard")
        if lb_cog:
            await lb_cog.paginate(interaction, direction=0)


# ============================================================
# HELPERS
# ============================================================

async def _match_from_thread(bot, interaction: discord.Interaction):
    """Look up the match associated with the current thread/channel."""
    if interaction.channel is None:
        return None
    thread_id = str(interaction.channel.id)
    async with bot.db._db.execute(
        "SELECT * FROM matches WHERE forum_thread_id = ?", (thread_id,)
    ) as cur:
        return await cur.fetchone()


def _is_staff(interaction: discord.Interaction) -> bool:
    staff_role_id = os.getenv("STAFF_ROLE_ID")
    if not staff_role_id:
        return False
    role = interaction.guild.get_role(int(staff_role_id))
    return role is not None and role in interaction.user.roles
