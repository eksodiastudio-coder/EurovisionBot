import os
import asyncio
import uuid
import traceback
import subprocess
from threading import Thread
from flask import Flask

import discord
import wave
import base64
from discord import app_commands
from discord.ext import commands
from google.genai import types

# --- Google GenAI SDK ---
from google import genai

# --- CONFIGURATION ---
STAFF_ROLE_ID = 1449084902539657288  
HR_ROLE_ID = 1460385491261194464  

EUROVISION_POINTS = [12, 10, 8, 7, 6, 5, 4, 3, 2, 1]

# --- BACKGROUND MUSIC PATHS ---
BGM_FILES = {
    "intro": "music/bgm_intro.mp3",       # Grand Evolvers Eurovision fanfare
    "jury": "music/bgm_jury.mp3",         # Tense, rhythmic jury beat
    "televote": "music/bgm_televote.mp3", # Escalating pulse track
    "winner": "music/bgm_winner.mp3"      # Celebratory victory theme
}

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
            
        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        if not member:
            return False

        if member.id == interaction.guild.owner_id or member.guild_permissions.administrator:
            return True

        return any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles)
    return app_commands.check(predicate)

def get_initial_points(candidate_count):
    if candidate_count < 10:
        return EUROVISION_POINTS[:candidate_count]
    return EUROVISION_POINTS.copy()

def generate_scoreboard_embed(poll_id, poll_data, reveal_staff=False, reveal_members=False):
    title = f"🏆 Evolvers Eurovision: {poll_data['title']} (ID: {poll_id})"
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


# --- GEMINI ACHIRD TTS CONFIGURATION ---
GEMINI_VOICE_NAME = "Achird"
TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

# Semaphore to prevent 429 API rate limits
TTS_SEMAPHORE = asyncio.Semaphore(1)

def mix_audio_with_bgm(tts_wav_path: str, bgm_key: str, bgm_volume: float = 0.20) -> str:
    """Mixes background music with smooth fade-in, voice ducking, and fade-out trail."""
    bgm_path = BGM_FILES.get(bgm_key)
    if not bgm_path or not os.path.exists(bgm_path):
        return tts_wav_path

    mixed_output_path = f"mixed_{uuid.uuid4().hex[:8]}.wav"
    try:
        with wave.open(tts_wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            voice_dur = frames / float(rate)

        total_dur = voice_dur + 1.2
        fade_out_start = max(0.1, total_dur - 0.7)

        cmd = [
            "ffmpeg", "-y",
            "-i", tts_wav_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex",
            f"[0:a]apad=pad_dur=1.2,volume=1.0[voice];"
            f"[1:a]volume={bgm_volume},afade=t=in:ss=0:d=0.5,afade=t=out:st={fade_out_start:.2f}:d=0.7[music];"
            f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2",
            "-c:a", "pcm_s16le",
            mixed_output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        if os.path.exists(tts_wav_path):
            os.remove(tts_wav_path)
            
        return mixed_output_path
    except Exception as e:
        print(f"[Audio Mixing Error for {bgm_key}]: {e}")
        return tts_wav_path


async def synthesize_tts_file(text: str, bgm_phase: str = None) -> str:
    """Synthesizes voice strictly with Achird using rate-limit safe queue."""
    if not text or not text.strip():
        return None

    temp_wav = f"gemini_tts_{uuid.uuid4().hex}.wav"
    audio_generated = False

    if not ai_client:
        print("[Gemini Error]: GEMINI_API_KEY not set.")
        return None

    tagged_text = f"[dramatic, game-show host energy] {text}"

    async with TTS_SEMAPHORE:
        for attempt in range(3):
            for model in TTS_MODELS:
                try:
                    response = await ai_client.aio.models.generate_content(
                        model=model,
                        contents=tagged_text,
                        config=types.GenerateContentConfig(
                            response_modalities=["AUDIO"],
                            speech_config=types.SpeechConfig(
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                        voice_name=GEMINI_VOICE_NAME
                                    )
                                )
                            )
                        )
                    )
                    
                    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                        for part in response.candidates[0].content.parts:
                            if part.inline_data and part.inline_data.data:
                                pcm_data = part.inline_data.data
                                if isinstance(pcm_data, str):
                                    pcm_data = base64.b64decode(pcm_data)
                                    
                                with wave.open(temp_wav, "wb") as wf:
                                    wf.setnchannels(1)
                                    wf.setsampwidth(2)
                                    wf.setframerate(24000)
                                    wf.writeframes(pcm_data)
                                    
                                audio_generated = True
                                break
                                
                    if audio_generated:
                        break
                except Exception as e:
                    print(f"[Gemini Achird TTS ({model}) Attempt {attempt+1}]: {e}")
                    await asyncio.sleep(0.5)

            if audio_generated:
                break
            await asyncio.sleep(1.0)

        # Brief pause between calls to respect API limits
        await asyncio.sleep(0.25)

    if audio_generated and os.path.exists(temp_wav):
        if bgm_phase:
            return await asyncio.to_thread(mix_audio_with_bgm, temp_wav, bgm_phase)
        return temp_wav

    return None


async def play_audio_file(voice_client: discord.VoiceClient, filepath: str):
    """Plays audio and waits until voice finishes speaking."""
    if not filepath or not os.path.exists(filepath):
        # Fallback delay if audio was missing
        await asyncio.sleep(3.0)
        return
        
    try:
        audio_source = discord.FFmpegPCMAudio(filepath)
        voice_client.play(audio_source)
        
        while voice_client.is_playing():
            await asyncio.sleep(0.08)
    except Exception as e:
        print(f"Error streaming to voice channel: {e}")


# --- CANDIDATE SETUP UI ---

class ServerMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select members from Evolvers to add to the candidate pool...",
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
                f"✨ **Configure Your Evolvers Candidates**\n\n"
                f"**Selected Nominees ({len(self.view.selected_users)}):**\n{candidate_list}\n\n"
                "You can select more members or click **Continue** to finalize."
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
            "staff_votes": {},
            "status": "members_voting",
            "message_id": None,
            "channel_id": interaction.channel_id
        }
        
        await interaction.response.edit_message(
            content=f"✅ **Evolvers Poll Setup Completed!**\n" + "\n".join([f"• {name}" for name in candidate_list]),
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

        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member and any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
                await interaction.response.send_message(
                    "❌ **Staff & HR Cannot Vote in Public Voting!**\nAs part of Evolvers Staff/HR, your votes will be cast during the **Staff Jury** voting phase instead.",
                    ephemeral=True
                )
                return

        user_id = str(interaction.user.id)
        if user_id in poll_data["member_votes"]:
            await interaction.response.send_message("❌ **You have already voted!**", ephemeral=True)
            return

        view = VotingView(self.poll_id, user_id, self.bot)
        await interaction.response.send_message(
            "🗳️ **Welcome to the Evolvers Voting Booth!**\n"
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
            await interaction.response.send_message("❌ **Permission Denied**: This phase is reserved for Evolvers Staff & HR.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id in poll_data["staff_votes"]:
            await interaction.response.send_message("❌ **You have already submitted your jury ballot!**", ephemeral=True)
            return

        staff_name = member.display_name
        view = StaffVotingView(self.poll_id, user_id, staff_name, self.bot)
        await interaction.response.send_message(
            f"🎙️ **Welcome {staff_name} to the Evolvers Staff Jury Booth!**\n"
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

@bot.tree.command(name="create_poll", description="Create a new Eurovision-style poll for Evolvers.")
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

@bot.tree.command(name="start_live_show", description="Host the Evolvers Eurovision Grand Final in a Voice Channel with Gemini!")
@app_commands.describe(poll_id="The ID of the poll", voice_channel="Voice channel where the show will be hosted")
@is_staff()
async def start_live_show(interaction: discord.Interaction, poll_id: str, voice_channel: discord.VoiceChannel):
    if poll_id not in bot.polls:
        await interaction.response.send_message("❌ Poll not found.", ephemeral=True)
        return
    
    poll_data = bot.polls[poll_id]
    poll_data["status"] = "live_show"
    await interaction.response.send_message(f"🎙️ **Preparing the Evolvers Grand Final Broadcast in {voice_channel.mention}...**")

    # Connect to Voice Channel
    try:
        vc = await voice_channel.connect()
    except Exception:
        vc = interaction.guild.voice_client

    text_channel = interaction.channel

    # --- 1. CALCULATE ALL TOTALS IN ADVANCE ---
    staff_breakdown_text = ""
    staff_total_points = {c: 0 for c in poll_data["candidates"]}
    for u_id, s_data in poll_data["staff_votes"].items():
        name = s_data["name"]
        ballot = s_data["ballot"]
        for c, pts in ballot.items():
            staff_total_points[c] += pts
        sorted_b = sorted(ballot.items(), key=lambda x: x[1], reverse=True)
        ballot_str = ", ".join([f"{c}: {p}pts" for c, p in sorted_b])
        staff_breakdown_text += f"\n- Juror '{name}': {ballot_str}"

    televote_totals = {c: 0 for c in poll_data["candidates"]}
    for u_id, b in poll_data["member_votes"].items():
        for c, pts in b.items():
            televote_totals[c] += pts
    sorted_televotes = sorted(televote_totals.items(), key=lambda x: x[1])

    combined_scores = {c: staff_total_points.get(c, 0) + televote_totals.get(c, 0) for c in poll_data["candidates"]}
    sorted_final = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
    
    winner_name = sorted_final[0][0] if sorted_final else "Nobody"
    winner_points = sorted_final[0][1] if sorted_final else 0
    runner_up = sorted_final[1][0] if len(sorted_final) > 1 else None

    # --- 2. BUILD STRUCTURED SEGMENTS ---
    staff_juries = list(poll_data["staff_votes"].values())

    # Build reliable structured speech items
    speech_items = []
    
    # Item 0: Intro
    intro_prompt = (
        f"Give a grand, energetic 2-3 sentence Eurovision opening welcoming the Evolvers Discord Server to tonight's "
        f"Grand Final of {poll_data['title']}. Do NOT use asterisks or markdown. Only spoken words."
    )
    speech_items.append({"prompt": intro_prompt, "phase": "intro", "fallback": f"Good evening Evolvers! Welcome to the Grand Final of {poll_data['title']}! What an electric night for our community. Let the show begin!"})

    # Items 1 to N: Staff Juries
    for jury in staff_juries:
        jury_name = jury["name"]
        sorted_b = sorted(jury["ballot"].items(), key=lambda x: x[1], reverse=True)
        top_c = sorted_b[0][0] if sorted_b else "the leader"
        
        j_prompt = (
            f"You are the host announcing the jury ballot from {jury_name}. Greet the juror, build suspense, "
            f"and announce their 12 points recipient using exact ellipses: 'And {jury_name}'s twelve points go to... ... ... {top_c}!'. "
            f"No asterisks or markdown."
        )
        speech_items.append({"prompt": j_prompt, "phase": "jury", "fallback": f"Let us hear from our esteemed jury member, {jury_name}. The tension is in the air. And {jury_name}'s twelve points go to... ... ... {top_c}!"})

    # Item N+1: Public Televotes
    tele_prompt = (
        f"Announce the start of the Evolvers public televotes. Remind everyone that public votes can change the entire scoreboard. "
        f"Keep it under 3 sentences. No asterisks or markdown."
    )
    speech_items.append({"prompt": tele_prompt, "phase": "televote", "fallback": "The jury votes are locked in! Now, the public televotes from the Evolvers community will decide the fate of our finalists."})

    # Item N+2: Grand Winner Coronation
    win_prompt = (
        f"Deliver the climactic winner coronation for the Evolvers Grand Final. "
        f"Build extreme suspense and crown {winner_name} with: 'And the ultimate champion of Evolvers is... ... ... {winner_name}!' with {winner_points} points. "
        f"Give a high-energy sign-off. No asterisks or markdown."
    )
    speech_items.append({"prompt": win_prompt, "phase": "winner", "fallback": f"Ladies and gentlemen of Evolvers, the moment of truth has arrived! And the champion of tonight is... ... ... {winner_name}! Congratulations to {winner_name}!"})

    # --- 3. GENERATE SCRIPT & SYNTHESIZE AUDIO SAFELY ---
    await text_channel.send("🎙️ *Synthesizing host broadcast & mixing Eurovision audio tracks with Achird...*")
    
    audio_files = []
    for item in speech_items:
        text_line = item["fallback"]
        if ai_client:
            try:
                res = await ai_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=item["prompt"]
                )
                if res.text and res.text.strip():
                    text_line = res.text.strip().replace("*", "").replace("#", "")
            except Exception as e:
                print(f"[Gemini Script Warning]: {e}")
        
        # Synthesize with phase BGM
        file_path = await synthesize_tts_file(text_line, bgm_phase=item["phase"])
        audio_files.append(file_path)

    # --- 4. LIVE BROADCAST EXECUTION (PERFECT 1:1 SYNC) ---
    try:
        # Act 1: Intro
        await text_channel.send("✨ **THE EVOLVERS GRAND FINAL BROADCAST IS NOW LIVE!** 🎙️")
        await play_audio_file(vc, audio_files[0])
        await asyncio.sleep(0.4)

        # Act 2: Staff Juries (One by One)
        for i, jury in enumerate(staff_juries, start=1):
            jury_name = jury["name"]
            sorted_ballot = sorted(jury["ballot"].items(), key=lambda x: x[1], reverse=True)
            
            # Voice speaks 12 points announcement FIRST
            if i < len(audio_files):
                await play_audio_file(vc, audio_files[i])
            
            # Text ballot revealed ONLY AFTER voice finishes
            ballot_display = "\n".join([f"• **{p} pts** ➡️ {c}" for c, p in sorted_ballot])
            await text_channel.send(f"🎙️ **Jury Ballot from {jury_name}:**\n{ballot_display}")
            await asyncio.sleep(0.4)

        # Act 3: Staff Scoreboard Standings
        embed_staff = generate_scoreboard_embed(poll_id, poll_data, reveal_staff=True, reveal_members=False)
        await text_channel.send("📊 **Scoreboard Standings after Jury Voting:**", embed=embed_staff)
        await asyncio.sleep(0.5)

        # Act 4: Public Televotes
        await text_channel.send("🗳️ **Now Announcing the Public Member Televotes!**")
        televote_idx = len(staff_juries) + 1
        if televote_idx < len(audio_files):
            await play_audio_file(vc, audio_files[televote_idx])

        televote_summary = "\n".join([f"• **{pts} pts** ➡️ {c}" for c, pts in sorted_televotes])
        await text_channel.send(f"📊 **Public Televote Points Added:**\n{televote_summary}")
        await asyncio.sleep(0.5)

        # Act 5: Grand Winner Coronation
        poll_data["status"] = "closed"
        await text_channel.send("🥁 **AND THE WINNER OF EVOLVERS IS...**")
        
        winner_idx = len(staff_juries) + 2
        if winner_idx < len(audio_files):
            await play_audio_file(vc, audio_files[winner_idx])

        # Final Embed and Congratulations posted strictly AFTER voice declares champion
        embed_final = generate_scoreboard_embed(poll_id, poll_data, reveal_staff=True, reveal_members=True)
        await text_channel.send(f"🏆 **🎉 CONGRATULATIONS TO OUR EVOLVERS CHAMPION: {winner_name}! 🎉**", embed=embed_final)

        await asyncio.sleep(3.0)
        await vc.disconnect()

    finally:
        for f in audio_files:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass


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
