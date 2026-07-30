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

# ── Match modes ──────────────────────────────────────────────────────────────
MATCH_MODES = {
    "Fist Only":    "👊 Fist Only — no abilities, just fists",
    "Same Ability": "🤝 Same Ability — both players use the same ability (vote below)",
    "F2P Ability":  "🆓 F2P Ability — free abilities only (vote below)",
    "P2W Ability":  "💎 P2W Ability — gamepass abilities only (vote below)",
}

FREE_ABILITIES = [
    "Wind", "Fire", "Water", "Rock", "Ice", "Lightning", "Psychic",
    "Archer", "Slime", "Ninja", "Brawler", "Pirate", "Beam", "Knight",
    "Hero", "Retro", "Shadow", "Vampire", "Blue Fire",
]

P2W_ABILITIES = [
    "Cursed", "Space Outlaw", "Angelic", "One Punch", "Sorcerer",
]

ALL_ABILITIES = FREE_ABILITIES + P2W_ABILITIES

# Modes that require an ability vote in the thread
ABILITY_VOTE_MODES = {"Same Ability", "F2P Ability", "P2W Ability"}

STATUS_LABELS = {
    "awaiting":          "🟡 Awaiting Match",
    "in_progress":       "🔵 Match in Progress",
    "awaiting_confirm":  "🟠 Awaiting Result Confirmation",
    "awaiting_forfeit":  "🏳️ Awaiting Forfeit Review",
    "completed":         "🟢 Completed",
    "disputed":          "🔴 Disputed — Staff Reviewing",
    "cancelled":         "⚫ Cancelled",
}

def _logger(interaction: discord.Interaction):
    """Shortcut to the LoggingCog helper."""
    return interaction.client.get_cog("Logging")


# ============================================================
# MODALS
# ============================================================

class ChallengeModal(discord.ui.Modal, title="⚔️ Create a Duel Challenge"):
    """Shown after the user picks a region and mode."""

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

    def __init__(self, region: str, mode: str = "Fist Only"):
        super().__init__()
        self.region = region
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        bot: DuelBot = interaction.client
        guild = interaction.guild

        challenger_roblox = self.challenger_roblox.value.strip()
        opponent_roblox   = self.opponent_roblox.value.strip()
        matched_region    = self.region
        opponent_id_raw   = self.opponent_discord.value.strip()

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
                "❌ That user is not a member of this server.", ephemeral=True,
            )
        if opponent_member.bot:
            return await interaction.response.send_message(
                "❌ You cannot challenge a bot.", ephemeral=True,
            )
        if opponent_member.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You cannot challenge yourself.", ephemeral=True,
            )

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
            match_mode=self.mode,
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
        choice  = self.winner_choice.value.strip().lower()

        if user_id not in (match["challenger_id"], match["opponent_id"]):
            return await interaction.response.send_message(
                "❌ You are not a participant in this match.", ephemeral=True
            )
        if match["status"] not in ("awaiting", "in_progress"):
            return await interaction.response.send_message(
                f"❌ This match cannot accept a result report. "
                f"Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )
        if match["reporter_id"] is not None:
            return await interaction.response.send_message(
                "❌ A result has already been reported for this match.", ephemeral=True
            )

        if choice == "me":
            winner_id = user_id
        elif choice in ("opponent", "them"):
            winner_id = match["opponent_id"] if user_id == match["challenger_id"] else match["challenger_id"]
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

        if not _is_staff(interaction):
            return await interaction.response.send_message(
                "❌ You do not have the Staff role required for this action.", ephemeral=True
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

        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.finalise_match(match, interaction.guild, how="staff_override")

        logger = _logger(interaction)
        if logger:
            await logger.log_staff_override(
                interaction.guild, match, interaction.user, old_status
            )

        # Edit the dispute channel message to mark it as handled
        await _mark_dispute_handled(
            bot,
            match["match_id"],
            f"✅ **{interaction.user.display_name}** has resolved this case — "
            f"Match **#{match['match_id']}** handled via staff override.",
        )

        await interaction.followup.send(
            f"✅ Override applied. Match **#{self.match_id}** resolved.", ephemeral=True
        )


# ============================================================
# PERSISTENT VIEWS
# ============================================================

class RegionSelectView(discord.ui.View):
    """Ephemeral region picker shown before the mode picker."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="1️⃣ Choose your region…",
        custom_id="region_select_dropdown",
        options=[discord.SelectOption(label=r, value=r) for r in REGIONS],
    )
    async def region_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        region = select.values[0]
        await interaction.response.edit_message(
            content=f"📍 Region: **{region}**\n\n⚔️ Now select your match mode:",
            view=ModeSelectView(region=region),
        )
        self.stop()


class ModeSelectView(discord.ui.View):
    """Ephemeral mode picker shown after region is chosen."""

    def __init__(self, region: str):
        super().__init__(timeout=120)
        self.region = region

    @discord.ui.select(
        placeholder="2️⃣ Choose match mode…",
        custom_id="mode_select_dropdown",
        options=[
            discord.SelectOption(label="👊 Fist Only",    value="Fist Only",    description="No abilities — fists only"),
            discord.SelectOption(label="🤝 Same Ability", value="Same Ability", description="Both players use the same ability (vote in thread)"),
            discord.SelectOption(label="🆓 F2P Ability",  value="F2P Ability",  description="Free abilities only (vote in thread)"),
            discord.SelectOption(label="💎 P2W Ability",  value="P2W Ability",  description="Gamepass abilities only (vote in thread)"),
        ],
    )
    async def mode_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        mode = select.values[0]
        await interaction.response.send_modal(ChallengeModal(region=self.region, mode=mode))
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
    """Attached to each forum post. Report / Forfeit / Cancel buttons."""

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
        label="🏳️ Forfeit",
        style=discord.ButtonStyle.secondary,
        custom_id="forfeit_btn",
    )
    async def forfeit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        user_id = str(interaction.user.id)

        # Only the challenged player (opponent) can forfeit
        if user_id != match["opponent_id"]:
            return await interaction.response.send_message(
                "❌ Only the challenged player can forfeit. "
                "If you want to withdraw your challenge, use **❌ Cancel Match**.",
                ephemeral=True,
            )
        if match["status"] not in ("awaiting", "in_progress"):
            return await interaction.response.send_message(
                f"❌ Cannot forfeit. Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        success = await bot.db.request_forfeit(match["match_id"], forfeiter_id=user_id)
        if not success:
            return await interaction.followup.send(
                "❌ Could not register forfeit. Try again.", ephemeral=True
            )

        match = await bot.db.get_match(match["match_id"])

        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(match)
            await matches_cog.post_forfeit_notice(match, interaction.guild, forfeiter=interaction.user)

        logger = _logger(interaction)
        if logger:
            await logger.log_forfeit_requested(interaction.guild, match, interaction.user)

        await interaction.followup.send(
            "🏳️ Forfeit submitted. Staff have been notified to review.", ephemeral=True
        )

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
        user_id        = str(interaction.user.id)
        is_participant = user_id in (match["challenger_id"], match["opponent_id"])
        is_staff_user  = _is_staff(interaction)
        if not is_participant and not is_staff_user:
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
                f"❌ No pending result to confirm. "
                f"Status: {STATUS_LABELS.get(match['status'], match['status'])}",
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

        # Edit the confirm/dispute message — remove buttons, show confirmed state
        try:
            await interaction.message.edit(
                content=f"✅ {interaction.user.mention} confirmed the result.",
                embed=None,
                view=discord.ui.View(),
            )
        except Exception:
            pass

        # Public message visible to everyone in the thread
        try:
            await interaction.channel.send(
                f"✅ Result confirmed by {interaction.user.mention}. Elo has been updated!"
            )
        except Exception:
            pass

        await interaction.followup.send("✅ Done.", ephemeral=True)

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
            # First dispute — warn, keep buttons, send public message
            try:
                await interaction.message.edit(
                    content=(
                        f"⚠️ {interaction.user.mention} disputed the result once.\n"
                        f"**Dispute once more to escalate to staff review.**\n"
                        f"The other player can still confirm if this was a mistake."
                    ),
                    view=ConfirmResultView(),
                )
            except Exception:
                pass

            try:
                await interaction.channel.send(
                    f"⚠️ {interaction.user.mention} has disputed the reported result. "
                    f"Dispute once more to call in staff."
                )
            except Exception:
                pass

            await interaction.followup.send(
                "⚠️ First dispute recorded. Dispute once more to escalate to staff.",
                ephemeral=True,
            )

        else:
            # Second dispute — remove buttons from confirm message, post to dispute channel
            try:
                await interaction.message.edit(
                    content=(
                        f"🔴 {interaction.user.mention} has disputed this result a second time. "
                        f"Staff are reviewing in the dispute channel."
                    ),
                    view=discord.ui.View(),  # remove buttons
                )
            except Exception:
                pass

            try:
                await interaction.channel.send(
                    f"🔴 This match has been escalated to staff. "
                    f"Staff are now reviewing the dispute in the designated channel."
                )
            except Exception:
                pass

            # Update forum post embed (no buttons) + post to dispute channel
            matches_cog = bot.get_cog("Matches")
            if matches_cog:
                await matches_cog.update_forum_post(match)
                await matches_cog.post_dispute_notice(match, interaction.guild, disputer=interaction.user)

            logger = _logger(interaction)
            if logger:
                await logger.log_result_disputed(interaction.guild, match, interaction.user)

            await interaction.followup.send(
                "🔴 Result disputed twice. Staff have been notified in the dispute channel.",
                ephemeral=True,
            )


class StaffOverrideView(discord.ui.View):
    """Staff-only override button, shown in the dispute channel."""

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


class StaffForfeitApprovalView(discord.ui.View):
    """
    Persistent staff buttons for approving/denying a forfeit request.
    Posted in the dispute channel. Survives restarts via embed Match ID lookup.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Approve Forfeit",
        style=discord.ButtonStyle.success,
        custom_id="forfeit_approve_btn",
    )
    async def approve_forfeit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not _is_staff(interaction):
            return await interaction.response.send_message(
                "❌ Staff only.", ephemeral=True
            )
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        if match["status"] != "awaiting_forfeit":
            return await interaction.response.send_message(
                f"❌ Match is not awaiting forfeit approval. "
                f"Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        success = await bot.db.approve_forfeit(match["match_id"])
        if not success:
            return await interaction.followup.send("❌ Approve failed.", ephemeral=True)

        match = await bot.db.get_match(match["match_id"])

        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.finalise_match(match, interaction.guild, how="forfeit")

        forfeiter_id   = match["forfeiter_id"]
        forfeit_count  = await bot.db.get_player_forfeit_count(forfeiter_id)
        repeat_warning = (
            f"\n⚠️ **Repeat offender** — <@{forfeiter_id}> has now forfeited **{forfeit_count}** time(s). "
            f"Consider reviewing for smurfing."
            if forfeit_count >= 2 else ""
        )

        # Edit the dispute channel message to mark it handled
        try:
            existing_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if existing_embed:
                existing_embed.colour = discord.Color.green()
                existing_embed.set_footer(text=f"✅ Resolved by {interaction.user.display_name}")
            await interaction.message.edit(
                content=(
                    f"✅ **{interaction.user.display_name}** has resolved this case — "
                    f"Forfeit approved for Match **#{match['match_id']}**."
                    f"{repeat_warning}"
                ),
                embed=existing_embed,
                view=discord.ui.View(),
            )
        except Exception:
            pass

        # Public announcement in forum thread
        if match["forum_thread_id"]:
            try:
                thread = bot.get_channel(int(match["forum_thread_id"])) or \
                         await bot.fetch_channel(int(match["forum_thread_id"]))
                await thread.send(
                    f"🏳️ Forfeit approved by staff. "
                    f"<@{forfeiter_id}> forfeits — <@{match['winner_id']}> wins!"
                    f"{repeat_warning}"
                )
            except Exception:
                pass

        logger = _logger(interaction)
        if logger:
            await logger.log_forfeit_approved(
                interaction.guild, match, interaction.user, forfeit_count
            )

        await interaction.followup.send("✅ Forfeit approved.", ephemeral=True)

    @discord.ui.button(
        label="❌ Deny Forfeit",
        style=discord.ButtonStyle.danger,
        custom_id="forfeit_deny_btn",
    )
    async def deny_forfeit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not _is_staff(interaction):
            return await interaction.response.send_message(
                "❌ Staff only.", ephemeral=True
            )
        bot: DuelBot = interaction.client
        match = await _match_from_thread(bot, interaction)
        if match is None:
            return await interaction.response.send_message(
                "❌ Could not find match.", ephemeral=True
            )
        if match["status"] != "awaiting_forfeit":
            return await interaction.response.send_message(
                f"❌ Match is not awaiting forfeit approval. "
                f"Status: {STATUS_LABELS.get(match['status'], match['status'])}",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        await bot.db.deny_forfeit(match["match_id"])
        match = await bot.db.get_match(match["match_id"])

        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(match)

        # Edit the dispute channel message to mark it handled
        try:
            existing_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if existing_embed:
                existing_embed.colour = discord.Color.orange()
                existing_embed.set_footer(text=f"❌ Denied by {interaction.user.display_name}")
            await interaction.message.edit(
                content=(
                    f"❌ **{interaction.user.display_name}** has resolved this case — "
                    f"Forfeit denied for Match **#{match['match_id']}**. The match continues."
                ),
                embed=existing_embed,
                view=discord.ui.View(),
            )
        except Exception:
            pass

        # Notify the forum thread
        if match["forum_thread_id"]:
            try:
                thread = bot.get_channel(int(match["forum_thread_id"])) or \
                         await bot.fetch_channel(int(match["forum_thread_id"]))
                await thread.send(
                    f"❌ Forfeit request denied by staff. The match continues! "
                    f"<@{match['challenger_id']}> <@{match['opponent_id']}>"
                )
            except Exception:
                pass

        await interaction.followup.send("❌ Forfeit denied.", ephemeral=True)


# ============================================================
# ABILITY VOTE VIEWS  (persistent — registered in bot.py setup_hook)
# ============================================================

async def _handle_ability_vote(
    interaction: discord.Interaction,
    ability: str,
):
    """Shared vote handler used by all three ability vote views."""
    bot = interaction.client
    match = await _match_from_thread(bot, interaction)
    if match is None:
        return await interaction.response.send_message(
            "❌ Could not find the match for this thread.", ephemeral=True
        )

    user_id = str(interaction.user.id)
    if user_id not in (match["challenger_id"], match["opponent_id"]):
        return await interaction.response.send_message(
            "❌ Only match participants can vote on the ability.", ephemeral=True
        )
    if match["status"] not in ("awaiting", "in_progress"):
        return await interaction.response.send_message(
            f"❌ Cannot vote — match status is {STATUS_LABELS.get(match['status'], match['status'])}.",
            ephemeral=True,
        )
    if match["chosen_ability"]:
        return await interaction.response.send_message(
            f"✅ Ability already decided: **{match['chosen_ability']}**", ephemeral=True
        )

    chosen = await bot.db.set_ability_vote(
        match["match_id"], user_id, ability,
        match["challenger_id"], match["opponent_id"],
    )

    if chosen:
        # Both players agreed — update the vote message and the forum embed
        await interaction.response.edit_message(
            content=f"✅ Both players agreed on **{chosen}**! This is the ability for this match.",
            view=discord.ui.View(),
        )
        # Refresh the main embed to show chosen ability
        updated_match = await bot.db.get_match(match["match_id"])
        matches_cog = bot.get_cog("Matches")
        if matches_cog:
            await matches_cog.update_forum_post(updated_match)
    else:
        updated = await bot.db.get_match(match["match_id"])
        c_vote = updated["challenger_ability_vote"] or "⏳ Not voted yet"
        o_vote = updated["opponent_ability_vote"] or "⏳ Not voted yet"
        await interaction.response.send_message(
            f"✅ Your vote for **{ability}** has been recorded!\n\n"
            f"**Current votes:**\n"
            f"• <@{updated['challenger_id']}>: {c_vote}\n"
            f"• <@{updated['opponent_id']}>: {o_vote}\n\n"
            f"Both players must vote for the **same** ability to confirm it.",
            ephemeral=True,
        )


class FreeAbilityVoteView(discord.ui.View):
    """Persistent ability vote view for F2P Ability mode."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="🆓 Vote for a free ability…",
        custom_id="free_ability_vote_select",
        options=[discord.SelectOption(label=a, value=a) for a in FREE_ABILITIES],
    )
    async def vote(self, interaction: discord.Interaction, select: discord.ui.Select):
        await _handle_ability_vote(interaction, select.values[0])


class P2WAbilityVoteView(discord.ui.View):
    """Persistent ability vote view for P2W Ability mode."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="💎 Vote for a gamepass ability…",
        custom_id="p2w_ability_vote_select",
        options=[discord.SelectOption(label=a, value=a) for a in P2W_ABILITIES],
    )
    async def vote(self, interaction: discord.Interaction, select: discord.ui.Select):
        await _handle_ability_vote(interaction, select.values[0])


class SameAbilityVoteView(discord.ui.View):
    """Persistent ability vote view for Same Ability mode (all abilities)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="🤝 Vote for an ability (any)…",
        custom_id="same_ability_vote_select",
        options=[discord.SelectOption(label=a, value=a) for a in ALL_ABILITIES],
    )
    async def vote(self, interaction: discord.Interaction, select: discord.ui.Select):
        await _handle_ability_vote(interaction, select.values[0])


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
    """
    Look up the match for the current context.

    Priority:
    1. Match whose forum_thread_id = current channel id  (forum thread context)
    2. Embed field "Match ID" on the interaction message  (dispute channel context)
    """
    if interaction.channel is not None:
        thread_id = str(interaction.channel.id)
        async with bot.db._db.execute(
            "SELECT * FROM matches WHERE forum_thread_id = ?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row

    # Fallback: read Match ID from the embed on the message that triggered the interaction
    if interaction.message and interaction.message.embeds:
        for embed in interaction.message.embeds:
            for field in embed.fields:
                if field.name == "Match ID":
                    match_id = field.value.lstrip("#").strip()
                    return await bot.db.get_match(match_id)

    return None


async def _mark_dispute_handled(bot, match_id: str, text: str):
    """
    Edit the dispute-channel message for `match_id` to show it's been handled.
    Silently does nothing if no dispute message was stored for this match.
    """
    info = await bot.db.get_dispute_message(match_id)
    if info is None:
        return
    try:
        ch = bot.get_channel(int(info["dispute_message_channel_id"]))
        if ch is None:
            ch = await bot.fetch_channel(int(info["dispute_message_channel_id"]))
        msg = await ch.fetch_message(int(info["dispute_message_id"]))
        existing_embed = msg.embeds[0] if msg.embeds else None
        if existing_embed:
            existing_embed.colour = discord.Color.green()
            existing_embed.set_footer(text="✅ Case closed")
        await msg.edit(content=text, embed=existing_embed, view=discord.ui.View())
    except Exception as exc:
        log.warning("Could not edit dispute channel message for match %s: %s", match_id, exc)


def _is_staff(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    staff_role_id = os.getenv("STAFF_ROLE_ID")
    if not staff_role_id:
        return False
    role = interaction.guild.get_role(int(staff_role_id))
    return role is not None and role in interaction.user.roles
