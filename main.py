import os
import discord
from discord import app_commands
from discord.ext import commands
import uuid
from flask import Flask
from threading import Thread

# --- CONFIGURATION ---
STAFF_ROLE_ID = 1449084902539657288  
HR_ROLE_ID = 1460385491261194464  # <-- Replace with your actual server HR Role ID

EUROVISION_POINTS = [12, 10, 8, 7, 6, 5, 4, 3, 2, 1]

class EurovisionBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True 
        super().__init__(command_prefix="!", intents=intents)
        self.polls = {}

    async def setup_hook(self):
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.errors.CheckFailure):
                await interaction.response.send_message(
                    "❌ **Permission Denied**: This command can only be used by server Staff or HR.", 
                    ephemeral=True
                )
            else:
                print(f"Command Error: {error}")

        await self.tree.sync()
        print("Slash commands synced.")

bot = EurovisionBot()

def is_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False
        # Allows access if the user has either the Staff role OR the HR role
        return any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles)
    return app_commands.check(predicate)

def get_initial_points(candidate_count):
    if candidate_count < 10:
        return EUROVISION_POINTS[:candidate_count]
    return EUROVISION_POINTS.copy()

def generate_scoreboard_embed(poll_id, poll_data, reveal_members=False):
    title = f"🏆 Poll: {poll_data['title']} (ID: {poll_id})"
    embed = discord.Embed(title=title, color=discord.Color.blue())
    
    scores = {candidate: 0 for candidate in poll_data["candidates"]}
    
    if poll_data["status"] in ["staff_voting", "closed"]:
        for user_votes in poll_data["staff_votes"].values():
            for candidate, points in user_votes.items():
                scores[candidate] += points

    if poll_data["type"] == "simple" and poll_data["status"] == "closed":
        for user_votes in poll_data["member_votes"].values():
            for candidate, points in user_votes.items():
                scores[candidate] += points
    elif poll_data["type"] == "hybrid" and reveal_members:
        for user_votes in poll_data["member_votes"].values():
            for candidate, points in user_votes.items():
                scores[candidate] += points

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    description = ""
    for rank, (candidate, score) in enumerate(sorted_scores, 1):
        description += f"**#{rank}** {candidate} — `{score} pts`\n"
    
    embed.description = description
    
    status_text = "Status: "
    if poll_data["status"] == "members_voting":
        status_text += "🟢 Public Voting Open"
    elif poll_data["status"] == "staff_voting":
        status_text += "🟡 Staff Live Voting in Progress"
    else:
        status_text += "🔴 Closed"
        
    embed.set_footer(text=f"{status_text} | Total member voters: {len(poll_data['member_votes'])}")
    return embed


# --- INTERACTIVE POLL SETUP UI ---

class ServerMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select members to add to the candidate pool...",
            min_values=1,
            max_values=25
        )

    async def callback(self, interaction: discord.Interaction):
        for user in self.values:
            if user not in self.view.selected_users:
                self.view.selected_users.append(user)
        
        candidate_list = "\n".join([f"• {user.display_name}" for user in self.view.selected_users])
        
        await interaction.response.edit_message(
            content=(
                f"✨ **Configure Your Candidates**\n\n"
                f"**Selected Candidates ({len(self.view.selected_users)}):**\n{candidate_list}\n\n"
                "You can select more candidates from the dropdown or click **Continue** to finalize."
            ),
            view=self.view
        )

class PollSetupView(discord.ui.View):
    def __init__(self, poll_id, title, poll_type, bot_instance):
        super().__init__(timeout=300)
        self.poll_id = poll_id
        self.title = title
        self.poll_type = poll_type
        self.bot = bot_instance
        self.selected_users = []
        
        self.add_item(ServerMemberSelect())

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, row=1)
    async def continue_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_users:
            await interaction.response.send_message(
                "❌ Please select at least one member to be a candidate before continuing.", 
                ephemeral=True
            )
            return

        candidate_list = [member.display_name for member in self.selected_users]
        
        self.bot.polls[self.poll_id] = {
            "title": self.title,
            "type": self.poll_type,
            "candidates": candidate_list,
            "member_votes": {},
            "staff_votes": {},
            "status": "members_voting",
            "message_id": None,
            "channel_id": interaction.channel_id
        }
        
        await interaction.response.edit_message(
            content=f"✅ **Poll Setup Completed!**\nSelected Candidates:\n" + "\n".join([f"• {name}" for name in candidate_list]),
            view=None
        )
        
        embed = generate_scoreboard_embed(self.poll_id, self.bot.polls[self.poll_id])
        board_view = PollBoardView(self.poll_id, self.bot)
        message = await interaction.channel.send(embed=embed, view=board_view)
        self.bot.polls[self.poll_id]["message_id"] = message.id


# --- ACTIVE SCOREBOARD VIEWS ---

class PollBoardView(discord.ui.View):
    def __init__(self, poll_id, bot_instance):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.bot = bot_instance

    @discord.ui.button(label="Vote", style=discord.ButtonStyle.primary, emoji="🗳️", custom_id="vote_button_persistent")
    async def vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.poll_id not in self.bot.polls:
            await interaction.response.send_message("❌ Poll data not found.", ephemeral=True)
            return
        
        poll_data = self.bot.polls[self.poll_id]
        
        if poll_data["status"] != "members_voting":
            await interaction.response.send_message("❌ Voting is closed for this poll or has moved to the next phase.", ephemeral=True)
            return

        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            # Prevent Staff and HR members from voting as public members
            if member and any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
                await interaction.response.send_message(
                    "❌ **Staff/HR Members Cannot Vote Here**: As a member of the Staff or HR team, you will cast your votes during the Live Staff Voting phase instead.", 
                    ephemeral=True
                )
                return

        user_id = str(interaction.user.id)
        
        if user_id in poll_data["member_votes"]:
            sorted_votes = sorted(poll_data["member_votes"][user_id].items(), key=lambda x: x[0])
            ballot_summary = "\n".join([f"**{p} pt** ➡️ {c}" for c, p in sorted_votes])
            await interaction.response.send_message(
                f"❌ **You have already given out your points!**\n\nYour submitted ballot:\n{ballot_summary}", 
                ephemeral=True
            )
            return

        view = VotingView(self.poll_id, user_id, self.bot)
        await interaction.response.send_message(
            "🗳️ **Welcome to the Voting Booth!**\n"
            "Select up to 5 candidates from the dropdown below. Each candidate will receive 1 point.\n"
            "*Note: If you dismiss this message before completing your ballot, your progress will reset and no points will count.*", 
            view=view, 
            ephemeral=True
        )


class StaffBoardView(discord.ui.View):
    def __init__(self, poll_id, bot_instance):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.bot = bot_instance

    @discord.ui.button(label="Staff Vote", style=discord.ButtonStyle.secondary, emoji="🎙️", custom_id="staff_vote_button_persistent")
    async def staff_vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.poll_id not in self.bot.polls:
            await interaction.response.send_message("❌ Poll data not found.", ephemeral=True)
            return
        
        poll_data = self.bot.polls[self.poll_id]
        
        if poll_data["status"] != "staff_voting":
            await interaction.response.send_message("❌ Staff voting is not active for this poll.", ephemeral=True)
            return

        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            # Require Staff or HR role to access the staff live voting
            if not member or not any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
                await interaction.response.send_message(
                    "❌ **Permission Denied**: This option is restricted to server Staff and HR.", 
                    ephemeral=True
                )
                return

        user_id = str(interaction.user.id)
        
        if user_id in poll_data["staff_votes"]:
            await interaction.response.send_message("❌ **You have already given out your points!**", ephemeral=True)
            return

        view = StaffVotingView(self.poll_id, user_id, self.bot)
        await interaction.response.send_message(
            "🎙️ **Welcome to the Live Staff Jury Booth!**\n"
            "Assign all your points to candidates. Once you have assigned the last point value, your votes will "
            "be officially posted in the channel and the live scoreboard will update.\n\n"
            "*Note: If you dismiss this message early, your session resets and no votes will be announced or recorded.*", 
            view=view, 
            ephemeral=True
        )


# --- INTERACTIVE MEMBER VOTING UI ---

class CandidateSelect(discord.ui.Select):
    def __init__(self, candidates):
        options = [discord.SelectOption(label=c, value=c) for c in candidates]
        super().__init__(placeholder="Select a candidate to assign points...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_candidate_selection(interaction, self.values[0])

class PointSelect(discord.ui.Select):
    def __init__(self, candidate, available_points):
        self.candidate_name = candidate
        options = [discord.SelectOption(label=f"{p} points", value=str(p)) for p in available_points]
        super().__init__(placeholder=f"Select points for {candidate}...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_point_selection(interaction, self.candidate_name, int(self.values[0]))

class ResetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start Over / Reset Ballot", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.current_ballot.clear()
        view.available_candidates = view.bot.polls[view.poll_id]["candidates"].copy()
        if hasattr(view, "available_points"):
            view.available_points = get_initial_points(len(view.available_candidates))
        view.update_to_candidate_select()
        await interaction.response.edit_message(content="🗑️ Your ballot has been reset! Let's start from the beginning.\n\nSelect your first candidate:", view=view)

class VotingView(discord.ui.View):
    def __init__(self, poll_id, user_id, bot_instance):
        super().__init__(timeout=900)
        self.poll_id = poll_id
        self.user_id = user_id
        self.bot = bot_instance
        
        poll_data = self.bot.polls[poll_id]
        self.available_candidates = poll_data["candidates"].copy()
        self.max_votes = min(5, len(self.available_candidates))
        self.current_ballot = {} 

        self.update_to_candidate_select()

    def update_to_candidate_select(self):
        self.clear_items()
        self.add_item(CandidateSelect(self.available_candidates))
        self.add_item(ResetButton())

    async def handle_candidate_selection(self, interaction: discord.Interaction, candidate: str):
        self.current_ballot[candidate] = 1
        self.available_candidates.remove(candidate)
        
        if len(self.current_ballot) >= self.max_votes or not self.available_candidates:
            self.clear_items()
            
            self.bot.polls[self.poll_id]["member_votes"][self.user_id] = self.current_ballot
            
            sorted_votes = sorted(self.current_ballot.items(), key=lambda x: x[0])
            ballot_summary = "\n".join([f"**1 pt** ➡️ {c}" for c, p in sorted_votes])
            self.add_item(ResetButton())
            
            await interaction.response.edit_message(
                content=f"✅ **Voting Complete!** Here is your final ballot:\n\n{ballot_summary}\n\n*Your votes are now recorded.*",
                view=self
            )
        else:
            self.update_to_candidate_select()
            await interaction.response.edit_message(
                content=f"✅ Recorded **1 point** for **{candidate}** ({len(self.current_ballot)}/{self.max_votes} selected).\n\nSelect your next candidate:",
                view=self
            )


# --- INTERACTIVE STAFF VOTING UI ---

class StaffCandidateSelect(discord.ui.Select):
    def __init__(self, candidates):
        options = [discord.SelectOption(label=c, value=c) for c in candidates]
        super().__init__(placeholder="Select a candidate to award points...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_candidate_selection(interaction, self.values[0])

class StaffPointSelect(discord.ui.Select):
    def __init__(self, candidate, available_points):
        self.candidate_name = candidate
        options = [discord.SelectOption(label=f"{p} points", value=str(p)) for p in available_points]
        super().__init__(placeholder=f"Select points for {candidate}...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_point_selection(interaction, self.candidate_name, int(self.values[0]))

class StaffVotingView(discord.ui.View):
    def __init__(self, poll_id, user_id, bot_instance):
        super().__init__(timeout=900)
        self.poll_id = poll_id
        self.user_id = user_id
        self.bot = bot_instance
        
        poll_data = self.bot.polls[poll_id]
        self.available_candidates = poll_data["candidates"].copy()
        self.available_points = get_initial_points(len(self.available_candidates))
        self.current_ballot = {}

        self.update_to_candidate_select()

    def update_to_candidate_select(self):
        self.clear_items()
        self.add_item(StaffCandidateSelect(self.available_candidates))

    async def handle_candidate_selection(self, interaction: discord.Interaction, candidate: str):
        self.clear_items()
        self.add_item(StaffPointSelect(self.available_points))
        await interaction.response.edit_message(content=f"Awarding points to **{candidate}**.\nSelect the points value:", view=self)

    async def handle_point_selection(self, interaction: discord.Interaction, candidate: str, points: int):
        self.current_ballot[candidate] = points
        self.available_candidates.remove(candidate)
        self.available_points.remove(points)
        
        poll_data = self.bot.polls[self.poll_id]
        channel = self.bot.get_channel(poll_data["channel_id"])
        
        if not self.available_candidates or not self.available_points:
            self.clear_items()
            
            poll_data["staff_votes"][self.user_id] = self.current_ballot
            
            sorted_votes = sorted(self.current_ballot.items(), key=lambda x: x[1], reverse=True)
            bullet_points = "\n".join([f"• **{p} pts** ➡️ {c}" for c, p in sorted_votes])
            
            await channel.send(
                content=f"🎙️ **Staff Jury Live Vote**: {interaction.user.mention} has cast their votes!\n\n{bullet_points}",
                delete_after=10
            )
            
            embed = generate_scoreboard_embed(self.poll_id, poll_data)
            message = await channel.fetch_message(poll_data["message_id"])
            await message.edit(embed=embed, view=StaffBoardView(self.poll_id, self.bot))
            
            await interaction.response.edit_message(
                content="✅ **Live Voting Complete!** Your points have been officially submitted, announced, and added to the board.",
                view=None
            )
        else:
            self.update_to_candidate_select()
            await interaction.response.edit_message(
                content=f"✅ Assigned **{points} points** to **{candidate}** locally.\n\nSelect the next candidate to vote for:",
                view=self
            )


# --- SLASH COMMANDS ---

@bot.tree.command(name="create_poll", description="Create a new Eurovision-style poll.")
@app_commands.describe(
    title="Name of the poll",
    poll_type="Hybrid (Staff + Member) or Simple (Member only)"
)
@app_commands.choices(poll_type=[
    app_commands.Choice(name="Hybrid (Eurovision style)", value="hybrid"),
    app_commands.Choice(name="Simple (Instant results)", value="simple")
])
@is_staff()
async def create_poll(interaction: discord.Interaction, title: str, poll_type: str):
    poll_id = str(uuid.uuid4())[:8]
    view = PollSetupView(poll_id, title, poll_type, bot)
    
    await interaction.response.send_message(
        "✨ **Configure Your Candidates**\n"
        "Use the dropdown menu below to select candidates to compete in this poll. "
        "You can add as many as you like. When finished, press **Continue**.",
        view=view,
        ephemeral=True
    )

@bot.tree.command(name="close_member_voting", description="Close the member voting phase.")
@is_staff()
async def close_member_voting(interaction: discord.Interaction, poll_id: str):
    if poll_id not in bot.polls:
        await interaction.response.send_message("Poll not found.", ephemeral=True)
        return
        
    poll_data = bot.polls[poll_id]
    
    if poll_data["status"] != "members_voting":
        await interaction.response.send_message("Poll is not in the member voting phase.", ephemeral=True)
        return
        
    if poll_data["type"] == "hybrid":
        poll_data["status"] = "staff_voting"
        await interaction.response.send_message("Member voting closed. Moving to Staff Live Voting phase.", ephemeral=True)
        
        embed = generate_scoreboard_embed(poll_id, poll_data)
        channel = bot.get_channel(poll_data["channel_id"])
        message = await channel.fetch_message(poll_data["message_id"])
        
        staff_view = StaffBoardView(poll_id, bot)
        await message.edit(embed=embed, view=staff_view)
        
    elif poll_data["type"] == "simple":
        poll_data["status"] = "closed"
        await interaction.response.send_message("Voting closed. Displaying final results.", ephemeral=True)
        
        embed = generate_scoreboard_embed(poll_id, poll_data)
        channel = bot.get_channel(poll_data["channel_id"])
        message = await channel.fetch_message(poll_data["message_id"])
        await message.edit(embed=embed, view=None)

@bot.tree.command(name="reveal_member_votes", description="Reveal and combine member votes to the board.")
@is_staff()
async def reveal_member_votes(interaction: discord.Interaction, poll_id: str):
    if poll_id not in bot.polls:
        await interaction.response.send_message("Poll not found.", ephemeral=True)
        return
        
    poll_data = bot.polls[poll_id]
    
    if poll_data["status"] != "staff_voting":
        await interaction.response.send_message("This action can only be taken after staff voting has concluded.", ephemeral=True)
        return
        
    poll_data["status"] = "closed"
    
    await interaction.response.send_message("🎉 **Member votes are now being revealed and added to the scoreboard!**")
    
    embed = generate_scoreboard_embed(poll_id, poll_data, reveal_members=True)
    channel = bot.get_channel(poll_data["channel_id"])
    message = await channel.fetch_message(poll_data["message_id"])
    await message.edit(embed=embed, view=None)


# --- WEB SERVER (for Render & UptimeRobot) ---

web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is running online."

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.start()


# --- STARTUP ---

if __name__ == "__main__":
    keep_alive()  # Start the web server
    
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        bot.run(token)
    else:
        print("Error: 'DISCORD_TOKEN' environment variable is not set.")
