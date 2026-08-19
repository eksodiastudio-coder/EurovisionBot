import os
import asyncio
import uuid
import traceback
from threading import Thread
from flask import Flask

import discord
from discord import app_commands
from discord.ext import commands
import edge_tts

# --- Google GenAI SDK ---
from google import genai

# --- CONFIGURATION ---
STAFF_ROLE_ID = 1449084902539657288  
HR_ROLE_ID = 1460385491261194464  

EUROVISION_POINTS = [12, 10, 8, 7, 6, 5, 4, 3, 2, 1]

# Initialize Gemini Client
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

class EurovisionBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True 
        intents.voice_states = True
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
        return any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles)
    return app_commands.check(predicate)

def get_initial_points(candidate_count):
    if candidate_count < 10:
        return EUROVISION_POINTS[:candidate_count]
    return EUROVISION_POINTS.copy()

def generate_scoreboard_embed(poll_id, poll_data, reveal_staff=False, reveal_members=False):
    title = f"🏆 Poll: {poll_data['title']} (ID: {poll_id})"
    embed = discord.Embed(title=title, color=discord.Color.gold())
    
    scores = {candidate: 0 for candidate in poll_data["candidates"]}
    
    if reveal_staff:
        for staff_info in poll_data["staff_votes"].values():
            for candidate, points in staff_info["ballot"].items():
                scores[candidate] += points

    if reveal_members:
        for user_votes in poll_data["member_votes"].values():
            for candidate, points in user_votes.items():
                scores[candidate] += points

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    description = ""
    for rank, (candidate, score) in enumerate(sorted_scores, 1):
        medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"**#{rank}**"
        description += f"{medal} {candidate} — `{score} pts`\n"
    
    embed.description = description
    
    status_text = "Status: "
    if poll_data["status"] == "members_voting":
        status_text += "🟢 Public Member Voting Open"
    elif poll_data["status"] == "staff_voting":
        status_text += "🟡 Secret Staff Jury Voting in Progress"
    elif poll_data["status"] == "live_show":
        status_text += "🎙️ Live Grand Final in Progress!"
    else:
        status_text += "🔴 Closed"
        
    embed.set_footer(text=f"{status_text} | Member ballots: {len(poll_data['member_votes'])} | Staff juries: {len(poll_data['staff_votes'])}")
    return embed


# --- HIGH-ENERGY AI VOICE HELPER ---

# Available Announcer Voices you can pick:
# 1. "en-US-GuyNeural"         -> Energetic American Game Show / TV Host (Recommended!)
# 2. "en-GB-RyanNeural"        -> Charismatic British Eurovision Broadcaster
# 3. "en-US-ChristopherNeural" -> Confident Radio Host / Announcer
# 4. "en-AU-WilliamNeural"     -> Upbeat Australian Presenter

ANNOUNCER_VOICE = "en-US-GuyNeural"

async def play_tts_audio(voice_client: discord.VoiceClient, text: str):
    """Generates expressive, high-energy neural audio and plays it in voice."""
    if not text.strip():
        return
        
    temp_file = f"tts_{uuid.uuid4().hex[:6]}.mp3"
    try:
        # rate="+10%" makes the delivery punchy and fast-paced
        # pitch="+4Hz" raises vocal excitement slightly like a real presenter
        communicate = edge_tts.Communicate(
            text=text,
            voice=ANNOUNCER_VOICE,
            rate="+10%",
            pitch="+4Hz"
        )
        await communicate.save(temp_file)
        
        audio_source = discord.FFmpegPCMAudio(temp_file)
        voice_client.play(audio_source)
        
        while voice_client.is_playing():
            await asyncio.sleep(0.3)
    except Exception as e:
        print(f"Error in TTS Playback: {e}")
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


# --- CANDIDATE SETUP UI ---

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
                "You can select more candidates or click **Continue** to finalize."
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
            await interaction.response.send_message("❌ Select at least one candidate.", ephemeral=True)
            return

        candidate_list = [member.display_name for member in self.selected_users]
        
        self.bot.polls[self.poll_id] = {
            "title": self.title,
            "type": self.poll_type,
            "candidates": candidate_list,
            "member_votes": {},
            "staff_votes": {},       # Structure: {user_id: {"name": str, "ballot": dict}}
            "status": "members_voting",
            "message_id": None,
            "channel_id": interaction.channel_id
        }
        
        await interaction.response.edit_message(
            content=f"✅ **Poll Setup Completed!**\n" + "\n".join([f"• {name}" for name in candidate_list]),
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

    @discord.ui.button(label="Vote (3 Choices)", style=discord.ButtonStyle.primary, emoji="🗳️", custom_id="vote_button_persistent")
    async def vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.poll_id not in self.bot.polls:
            await interaction.response.send_message("❌ Poll data not found.", ephemeral=True)
            return
        
        poll_data = self.bot.polls[self.poll_id]
        
        if poll_data["status"] != "members_voting":
            await interaction.response.send_message("❌ Public member voting is closed.", ephemeral=True)
            return

        # BLOCK STAFF/HR FROM VOTING IN MEMBER VOTING
        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member and any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
                await interaction.response.send_message(
                    "❌ **Staff & HR Cannot Vote in Public Voting!**\nAs a member of Staff/HR, you will cast your points during the **Staff Jury** voting phase instead.",
                    ephemeral=True
                )
                return

        user_id = str(interaction.user.id)
        if user_id in poll_data["member_votes"]:
            await interaction.response.send_message("❌ **You have already voted!**", ephemeral=True)
            return

        view = VotingView(self.poll_id, user_id, self.bot)
        await interaction.response.send_message(
            "🗳️ **Welcome to the Voting Booth!**\n"
            "Select **up to 3 candidates**. Each candidate will receive 1 point.",
            view=view, 
            ephemeral=True
        )


class StaffBoardView(discord.ui.View):
    def __init__(self, poll_id, bot_instance):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.bot = bot_instance

    @discord.ui.button(label="Staff Jury Vote", style=discord.ButtonStyle.secondary, emoji="🎙️", custom_id="staff_vote_btn_persistent")
    async def staff_vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.poll_id not in self.bot.polls:
            await interaction.response.send_message("❌ Poll data not found.", ephemeral=True)
            return
        
        poll_data = self.bot.polls[self.poll_id]
        if poll_data["status"] != "staff_voting":
            await interaction.response.send_message("❌ Staff voting is not active.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
            await interaction.response.send_message("❌ **Permission Denied**: This phase is reserved for Staff & HR.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id in poll_data["staff_votes"]:
            await interaction.response.send_message("❌ **You have already submitted your jury ballot!**", ephemeral=True)
            return

        staff_name = member.display_name
        view = StaffVotingView(self.poll_id, user_id, staff_name, self.bot)
        await interaction.response.send_message(
            f"🎙️ **Welcome {staff_name} to the Staff Jury Booth!**\n"
            "Assign all your Eurovision points. Your ballot will be announced live on stage during the grand final!",
            view=view, 
            ephemeral=True
        )


# --- VOTING VIEWS ---

class CandidateSelect(discord.ui.Select):
    def __init__(self, candidates):
        options = [discord.SelectOption(label=c, value=c) for c in candidates]
        super().__init__(placeholder="Select a candidate...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_candidate_selection(interaction, self.values[0])

class VotingView(discord.ui.View):
    def __init__(self, poll_id, user_id, bot_instance):
        super().__init__(timeout=900)
        self.poll_id = poll_id
        self.user_id = user_id
        self.bot = bot_instance
        
        poll_data = self.bot.polls[poll_id]
        self.available_candidates = poll_data["candidates"].copy()
        self.max_votes = min(3, len(self.available_candidates))
        self.current_ballot = {} 

        self.update_to_candidate_select()

    def update_to_candidate_select(self):
        self.clear_items()
        self.add_item(CandidateSelect(self.available_candidates))

    async def handle_candidate_selection(self, interaction: discord.Interaction, candidate: str):
        self.current_ballot[candidate] = 1
        self.available_candidates.remove(candidate)
        
        if len(self.current_ballot) >= self.max_votes or not self.available_candidates:
            self.clear_items()
            self.bot.polls[self.poll_id]["member_votes"][self.user_id] = self.current_ballot
            
            ballot_summary = "\n".join([f"• **1 pt** ➡️ {c}" for c in self.current_ballot.keys()])
            await interaction.response.edit_message(
                content=f"✅ **Voting Complete!** Your submitted ballot:\n\n{ballot_summary}",
                view=None
            )
        else:
            self.update_to_candidate_select()
            await interaction.response.edit_message(
                content=f"✅ Selected **{candidate}** ({len(self.current_ballot)}/{self.max_votes}).\nSelect next candidate:",
                view=self
            )


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
    def __init__(self, poll_id, user_id, staff_name, bot_instance):
        super().__init__(timeout=900)
        self.poll_id = poll_id
        self.user_id = user_id
        self.staff_name = staff_name
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
        self.add_item(StaffPointSelect(candidate, self.available_points))
        await interaction.response.edit_message(content=f"Awarding points to **{candidate}**.\nSelect point value:", view=self)

    async def handle_point_selection(self, interaction: discord.Interaction, candidate: str, points: int):
        self.current_ballot[candidate] = points
        self.available_candidates.remove(candidate)
        self.available_points.remove(points)
        
        poll_data = self.bot.polls[self.poll_id]
        
        if not self.available_candidates or not self.available_points:
            self.clear_items()
            # Store staff display name along with their ballot
            poll_data["staff_votes"][self.user_id] = {
                "name": self.staff_name,
                "ballot": self.current_ballot
            }
            
            await interaction.response.edit_message(
                content="✅ **Jury Ballot Recorded!** Your votes are safely stored and will be revealed during the live show.",
                view=None
            )
        else:
            self.update_to_candidate_select()
            await interaction.response.edit_message(
                content=f"✅ Assigned **{points} points** to **{candidate}**.\n\nSelect next candidate:",
                view=self
            )


# --- SLASH COMMANDS ---

@bot.tree.command(name="create_poll", description="Create a new Eurovision-style poll.")
@app_commands.describe(title="Name of the poll", poll_type="Hybrid (Staff + Member) or Simple")
@app_commands.choices(poll_type=[
    app_commands.Choice(name="Hybrid (Eurovision style)", value="hybrid"),
    app_commands.Choice(name="Simple (Member only)", value="simple")
])
@is_staff()
async def create_poll(interaction: discord.Interaction, title: str, poll_type: str):
    poll_id = str(uuid.uuid4())[:8]
    view = PollSetupView(poll_id, title, poll_type, bot)
    await interaction.response.send_message("✨ **Configure Candidates** via dropdown, then click Continue.", view=view, ephemeral=True)

@bot.tree.command(name="start_staff_voting", description="Close member voting and open hidden staff jury voting.")
@is_staff()
async def start_staff_voting(interaction: discord.Interaction, poll_id: str):
    if poll_id not in bot.polls:
        await interaction.response.send_message("❌ Poll not found.", ephemeral=True)
        return
    poll_data = bot.polls[poll_id]
    poll_data["status"] = "staff_voting"
    
    embed = generate_scoreboard_embed(poll_id, poll_data)
    channel = bot.get_channel(poll_data["channel_id"])
    message = await channel.fetch_message(poll_data["message_id"])
    await message.edit(embed=embed, view=StaffBoardView(poll_id, bot))
    await interaction.response.send_message("✅ Public member voting is closed. Staff jury voting is now open (votes remain hidden until the show!).")

@bot.tree.command(name="start_live_show", description="Host the Eurovision Grand Final via Voice Channel with Gemini!")
@app_commands.describe(poll_id="The ID of the poll", voice_channel="Voice channel where the show will be hosted")
@is_staff()
async def start_live_show(interaction: discord.Interaction, poll_id: str, voice_channel: discord.VoiceChannel):
    if poll_id not in bot.polls:
        await interaction.response.send_message("❌ Poll not found.", ephemeral=True)
        return
    
    poll_data = bot.polls[poll_id]
    poll_data["status"] = "live_show"
    await interaction.response.send_message(f"🎙️ **Starting the Live Grand Final in {voice_channel.mention}!**")

    # Connect to Voice Channel
    try:
        vc = await voice_channel.connect()
    except Exception:
        vc = interaction.guild.voice_client

    text_channel = interaction.channel

    # Format staff ballots for the prompt
    staff_breakdown_text = ""
    for u_id, s_data in poll_data["staff_votes"].items():
        name = s_data["name"]
        ballot = s_data["ballot"]
        sorted_b = sorted(ballot.items(), key=lambda x: x[1], reverse=True)
        ballot_str = ", ".join([f"{c}: {p}pts" for c, p in sorted_b])
        staff_breakdown_text += f"\n- Staff Juror '{name}': {ballot_str}"

    # Calculate public televotes
    televote_totals = {c: 0 for c in poll_data["candidates"]}
    for u_id, b in poll_data["member_votes"].items():
        for c, pts in b.items():
            televote_totals[c] += pts
    sorted_televotes = sorted(televote_totals.items(), key=lambda x: x[1])

    # 1. Ask Gemini to write an exciting script
    prompt = f"""
    You are the iconic, enthusiastic, and electric host of the Eurovision Song Contest Grand Final.
    Tonight is the big reveal show for: '{poll_data['title']}'!
    
    DATA:
    - Candidates: {poll_data['candidates']}
    - Staff Juries and their specific votes:{staff_breakdown_text}
    - Public Member Televotes (total points received per candidate): {sorted_televotes}
    
    INSTRUCTIONS FOR SCRIPT:
    Produce clean spoken lines for text-to-speech. Do not use asterisks, markdown, emojis, or sound effect brackets.
    Divide your response into distinct sections using the exact delimiter '---SECTION---'.
    
    1. SECTION 1 (INTRO): Give a thrilling, high-energy Eurovision greeting to the server! Hype up the final results.
    
    2. SECTION 2 TO (N+1) (ONE SECTION PER STAFF JURY):
       For each staff juror, create a section where you say:
       "Let us now see the votes from [Staff Name]!"
       Announce their lower point awards briefly, then build massive suspense and announce:
       "And [Staff Name]'s twelve points go to... [Candidate Name]!"
    
    3. NEXT SECTION (TELEVOTES):
       Announce the public community televotes from lowest to highest.
    
    4. FINAL SECTION (WINNER):
       Crown the ultimate Member of the Month champion with fireworks energy, thank everyone, and close the show!
    """

    host_script = ""
    if ai_client:
        try:
            response = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            host_script = response.text
        except Exception as e:
            print(f"Gemini API Error: {e}")

    # Parse sections
    if host_script and "---SECTION---" in host_script:
        sections = [s.strip() for s in host_script.split("---SECTION---") if s.strip()]
    else:
        # Fallback if AI generation encounters issues
        sections = [
            f"Good evening and welcome to the Grand Final of {poll_data['title']}! The votes are in, and the tension is electric!"
        ]
        for u_id, s_data in poll_data["staff_votes"].items():
            name = s_data["name"]
            sorted_b = sorted(s_data["ballot"].items(), key=lambda x: x[1], reverse=True)
            top_candidate = sorted_b[0][0] if sorted_b else "the leader"
            sections.append(f"Here are the votes from our jury member {name}. And {name}'s twelve points go to... {top_candidate}!")
        sections.append("And now it is time for the public member televotes! Let us see what the community decided.")
        sections.append("What a night! Congratulations to our winner of Member of the Month! Goodnight everyone!")

    # 2. RUN THE SHOW LIVE
    # Intro
    await text_channel.send("🎉 **THE GRAND FINAL IS LIVE!** 🎙️")
    await play_tts_audio(vc, sections[0])
    await asyncio.sleep(2)

    # Step through staff juries
    staff_juries = list(poll_data["staff_votes"].values())
    section_index = 1
    
    for jury in staff_juries:
        jury_name = jury["name"]
        ballot = jury["ballot"]
        sorted_ballot = sorted(ballot.items(), key=lambda x: x[1], reverse=True)
        
        ballot_display = "\n".join([f"• **{p} pts** ➡️ {c}" for c, p in sorted_ballot])
        await text_channel.send(f"🎙️ **Jury Votes from {jury_name}:**\n{ballot_display}")
        
        if section_index < len(sections):
            await play_tts_audio(vc, sections[section_index])
            section_index += 1
        await asyncio.sleep(2)

    # Show Staff Scoreboard
    embed_staff = generate_scoreboard_embed(poll_id, poll_data, reveal_staff=True, reveal_members=False)
    await text_channel.send("📊 **Scoreboard after Staff Jury Voting:**", embed=embed_staff)
    await asyncio.sleep(3)

    # Televotes
    await text_channel.send("🗳️ **Now Revealing the Public Member Televotes!**")
    if section_index < len(sections):
        await play_tts_audio(vc, sections[section_index])
        section_index += 1
    await asyncio.sleep(2)

    # Winner & Final Scoreboard
    poll_data["status"] = "closed"
    embed_final = generate_scoreboard_embed(poll_id, poll_data, reveal_staff=True, reveal_members=True)
    await text_channel.send("🏆 **THE GRAND FINAL WINNER & FINAL STANDINGS!**", embed=embed_final)
    
    if section_index < len(sections):
        await play_tts_audio(vc, sections[section_index])

    await asyncio.sleep(4)
    await vc.disconnect()


# --- WEB SERVER (For 24/7 Hosting) ---

web_app = Flask('')

@web_app.route('/')
def home():
    return "Eurovision Bot Online."

def keep_alive():
    t = Thread(target=lambda: web_app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080))))
    t.start()


if __name__ == "__main__":
    keep_alive()
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        bot.run(token)
    else:
        print("Error: 'DISCORD_TOKEN' not set.")
