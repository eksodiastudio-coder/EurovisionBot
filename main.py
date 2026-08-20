import os
import asyncio
import uuid
import wave
import base64
import subprocess
from threading import Thread
from flask import Flask

import discord
from discord import app_commands
from discord.ext import commands

# --- Google GenAI SDK ---
from google import genai
from google.genai import types

# --- CONFIGURATION ---
STAFF_ROLE_ID = 1449084902539657288  
HR_ROLE_ID = 1460385491261194464  

EUROVISION_POINTS = [12, 10, 8, 7, 6, 5, 4, 3, 2, 1]

# Background music files (Place inside a /music folder or bot directory)
BGM_FILES = {
    "intro": "music/bgm_intro.mp3",       # Grand Eurovision fanfare
    "jury": "music/bgm_jury.mp3",         # Tense, rhythmic jury beat
    "televote": "music/bgm_televote.mp3", # Escalating pulse
    "winner": "music/bgm_winner.mp3"      # Victory theme
}

GEMINI_VOICE_NAME = "Achird"
TTS_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash"]

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
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ **Permission Denied**: This command can only be used by server Staff or HR.", 
                        ephemeral=True
                    )
            else:
                print(f"[Command Error]: {error}")

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
        status_text += "🟡 Staff Jury Voting in Progress"
    elif poll_data["status"] == "live_show":
        status_text += "🎙️ Live Grand Final in Progress!"
    else:
        status_text += "🔴 Closed"
        
    embed.set_footer(text=f"{status_text} | Member ballots: {len(poll_data['member_votes'])} | Staff juries: {len(poll_data['staff_votes'])}")
    return embed


# --- AUDIO SYNTHESIS & MIXING ---

def mix_audio_with_bgm(tts_wav_path: str, bgm_key: str, bgm_volume: float = 0.20) -> str:
    """Mixes background music under Achird's TTS speech using FFmpeg ducking."""
    bgm_path = BGM_FILES.get(bgm_key)
    if not bgm_path or not os.path.exists(bgm_path):
        return tts_wav_path

    mixed_output_path = f"mixed_{uuid.uuid4().hex[:8]}.wav"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", tts_wav_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex",
            f"[0:a]volume=1.0[voice];[1:a]volume={bgm_volume}[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2",
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


async def synthesize_achird_tts(text: str, bgm_phase: str = None, retries: int = 3) -> str:
    """Strictly synthesizes voice audio with Gemini's Achird voice and mixes background music."""
    if not text or not text.strip():
        return None

    if not ai_client:
        raise ValueError("GEMINI_API_KEY environment variable is not configured.")

    clean_text = text.replace("*", "").replace("#", "").replace("[", "").replace("]", "").strip()
    tagged_text = f"[dramatic, grand eurovision host voice] {clean_text}"

    temp_wav = f"gemini_achird_{uuid.uuid4().hex}.wav"
    audio_bytes = None

    for attempt in range(1, retries + 1):
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
                            raw_data = part.inline_data.data
                            if isinstance(raw_data, str):
                                audio_bytes = base64.b64decode(raw_data)
                            else:
                                audio_bytes = raw_data
                            break

                if audio_bytes:
                    break
            except Exception as e:
                print(f"[Achird TTS {model} Attempt {attempt} Error]: {e}")

        if audio_bytes:
            break
        await asyncio.sleep(2 * attempt)

    if not audio_bytes:
        raise RuntimeError(f"Failed to generate Achird TTS audio after {retries} attempts.")

    # Write 24kHz mono 16-bit PCM WAV
    with wave.open(temp_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(audio_bytes)

    if bgm_phase:
        return await asyncio.to_thread(mix_audio_with_bgm, temp_wav, bgm_phase)

    return temp_wav


async def play_audio_file(voice_client: discord.VoiceClient, filepath: str):
    """Accurately streams the audio file to Discord voice without cutting off."""
    if not filepath or not os.path.exists(filepath):
        return

    finished_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def after_playing(error):
        if error:
            print(f"[Playback Error]: {error}")
        loop.call_soon_threadsafe(finished_event.set)

    audio_source = discord.FFmpegPCMAudio(filepath)
    voice_client.play(audio_source, after=after_playing)
    await finished_event.wait()


# --- UI VIEWS ---

class ServerMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select members to add as candidates...", min_values=1, max_values=25)

    async def callback(self, interaction: discord.Interaction):
        for user in self.values:
            if user not in self.view.selected_users:
                self.view.selected_users.append(user)
        
        candidate_list = "\n".join([f"• {user.display_name}" for user in self.view.selected_users])
        await interaction.response.edit_message(
            content=(
                f"✨ **Configure Candidates ({len(self.view.selected_users)} selected):**\n"
                f"{candidate_list}\n\nSelect more or click **Continue** to finalize."
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
            content=f"✅ **Poll Setup Completed!**\n" + "\n".join([f"• {name}" for name in candidate_list]),
            view=None
        )
        
        embed = generate_scoreboard_embed(self.poll_id, self.bot.polls[self.poll_id])
        board_view = PollBoardView(self.poll_id, self.bot)
        message = await interaction.channel.send(embed=embed, view=board_view)
        self.bot.polls[self.poll_id]["message_id"] = message.id


class PollBoardView(discord.ui.View):
    def __init__(self, poll_id, bot_instance):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.bot = bot_instance

    @discord.ui.button(label="Vote (3 Choices)", style=discord.ButtonStyle.primary, emoji="🗳️", custom_id="vote_btn")
    async def vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.poll_id not in self.bot.polls:
            await interaction.response.send_message("❌ Poll data not found.", ephemeral=True)
            return
        
        poll_data = self.bot.polls[self.poll_id]
        if poll_data["status"] != "members_voting":
            await interaction.response.send_message("❌ Member voting is closed.", ephemeral=True)
            return

        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member and any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
                await interaction.response.send_message("❌ Staff & HR vote during the hidden Staff Jury phase.", ephemeral=True)
                return

        user_id = str(interaction.user.id)
        if user_id in poll_data["member_votes"]:
            await interaction.response.send_message("❌ You have already voted!", ephemeral=True)
            return

        view = VotingView(self.poll_id, user_id, self.bot)
        await interaction.response.send_message(
            "🗳️ Select **up to 3 candidates**. Each candidate will receive 1 point.",
            view=view, 
            ephemeral=True
        )


class StaffBoardView(discord.ui.View):
    def __init__(self, poll_id, bot_instance):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.bot = bot_instance

    @discord.ui.button(label="Staff Jury Vote", style=discord.ButtonStyle.secondary, emoji="🎙️", custom_id="staff_vote_btn")
    async def staff_vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.poll_id not in self.bot.polls:
            await interaction.response.send_message("❌ Poll data not found.", ephemeral=True)
            return
        
        poll_data = self.bot.polls[self.poll_id]
        if poll_data["status"] != "staff_voting":
            await interaction.response.send_message("❌ Staff jury voting is not active.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles):
            await interaction.response.send_message("❌ This phase is reserved for Staff & HR.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id in poll_data["staff_votes"]:
            await interaction.response.send_message("❌ You have already submitted your jury ballot.", ephemeral=True)
            return

        view = StaffVotingView(self.poll_id, user_id, member.display_name, self.bot)
        await interaction.response.send_message(
            f"🎙️ Welcome **{member.display_name}**! Assign your Eurovision points. Your ballot will be revealed live during the show.",
            view=view, 
            ephemeral=True
        )


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
                content=f"✅ **Ballot Submitted!**\n\n{ballot_summary}",
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
        super().__init__(placeholder="Select candidate to award points...", options=options)

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
                content="✅ **Jury Ballot Recorded!** Your points are saved for the live broadcast.",
                view=None
            )
        else:
            self.update_to_candidate_select()
            await interaction.response.edit_message(
                content=f"✅ Assigned **{points} points** to **{candidate}**.\nSelect next candidate:",
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
    await interaction.response.send_message("✅ Public voting closed. Staff jury voting is now open (hidden until broadcast).")


@bot.tree.command(name="start_live_show", description="Host the Eurovision Grand Final via Voice Channel with Achird!")
@app_commands.describe(poll_id="The ID of the poll", voice_channel="Voice channel where the show will be hosted")
@is_staff()
async def start_live_show(interaction: discord.Interaction, poll_id: str, voice_channel: discord.VoiceChannel):
    if poll_id not in bot.polls:
        await interaction.response.send_message("❌ Poll not found.", ephemeral=True)
        return
    
    poll_data = bot.polls[poll_id]
    poll_data["status"] = "live_show"
    
    # Acknowledge immediately
    await interaction.response.send_message(f"⏳ **Preparing all broadcast scenes with Achird voice... Please wait.**")
    progress_msg = await interaction.original_response()

    # --- 1. CALCULATE SCORE STANDINGS ---
    staff_total_points = {c: 0 for c in poll_data["candidates"]}
    for u_id, s_data in poll_data["staff_votes"].items():
        for c, pts in s_data["ballot"].items():
            staff_total_points[c] += pts

    televote_totals = {c: 0 for c in poll_data["candidates"]}
    for u_id, b in poll_data["member_votes"].items():
        for c, pts in b.items():
            televote_totals[c] += pts
    sorted_televotes = sorted(televote_totals.items(), key=lambda x: x[1])

    combined_scores = {c: staff_total_points.get(c, 0) + televote_totals.get(c, 0) for c in poll_data["candidates"]}
    sorted_final = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
    winner_name = sorted_final[0][0] if sorted_final else "Nobody"
    winner_points = sorted_final[0][1] if sorted_final else 0

    # --- 2. BUILD STRUCTURED BROADCAST SCENES ---
    scenes = []

    # Scene 1: Intro
    scenes.append({
        "type": "intro",
        "bgm": "intro",
        "script": (
            f"Good evening Europe, good evening world, and welcome to the Grand Final of {poll_data['title']}! "
            f"Tonight, we find out who takes the crown. Let the voting begin!"
        )
    })

    # Scene 2 to N: Each Staff Juror
    for jury_data in poll_data["staff_votes"].values():
        name = jury_data["name"]
        sorted_b = sorted(jury_data["ballot"].items(), key=lambda x: x[1], reverse=True)
        twelve_pts_candidate = sorted_b[0][0] if sorted_b else "the nominee"
        
        scenes.append({
            "type": "jury",
            "bgm": "jury",
            "jury_data": jury_data,
            "script": (
                f"We now cross live to our esteemed jury member, {name}. "
                f"Thank you, {name}. And {name}'s coveted twelve points go to... {twelve_pts_candidate}!"
            )
        })

    # Scene N+1: Televote Announcement
    scenes.append({
        "type": "televote",
        "bgm": "televote",
        "script": (
            "The jury votes are locked in. But this competition is far from over! "
            "It is now time for the public member televotes. Every single vote counts!"
        )
    })

    # Scene N+2: Grand Winner
    scenes.append({
        "type": "winner",
        "bgm": "winner",
        "script": (
            f"Ladies and gentlemen, the moment of truth has arrived! "
            f"With a spectacular total of {winner_points} points, the winner of {poll_data['title']} is... {winner_name}! "
            f"Congratulations to {winner_name}, and thank you all for being part of tonight's grand final! Goodnight!"
        )
    })

    # --- 3. PRE-SYNTHESIZE ALL SCENES (STRICTLY ACHIRD) ---
    generated_audio = []
    try:
        for idx, sc in enumerate(scenes, 1):
            await progress_msg.edit(content=f"🎙️ **Synthesizing Scene {idx}/{len(scenes)} with Achird voice & mixing BGM...**")
            audio_path = await synthesize_achird_tts(sc["script"], bgm_phase=sc["bgm"])
            generated_audio.append(audio_path)
            sc["audio_file"] = audio_path

        await progress_msg.edit(content=f"✅ **All scenes ready! Connecting to {voice_channel.mention} to start the broadcast...**")

        # --- 4. CONNECT TO VOICE CHANNEL ---
        try:
            vc = await voice_channel.connect()
        except Exception:
            vc = interaction.guild.voice_client

        text_channel = interaction.channel

        # --- 5. EXECUTE LIVE BROADCAST ---
        await text_channel.send("✨ **THE GRAND FINAL BROADCAST IS NOW LIVE!** 🎙️")

        for sc in scenes:
            scene_type = sc["type"]
            audio_file = sc["audio_file"]

            if scene_type == "intro":
                await play_audio_file(vc, audio_file)
                await asyncio.sleep(0.5)

            elif scene_type == "jury":
                await play_audio_file(vc, audio_file)
                jury_info = sc["jury_data"]
                sorted_ballot = sorted(jury_info["ballot"].items(), key=lambda x: x[1], reverse=True)
                ballot_display = "\n".join([f"• **{p} pts** ➡️ {c}" for c, p in sorted_ballot])
                await text_channel.send(f"🎙️ **Jury Ballot from {jury_info['name']}:**\n{ballot_display}")
                await asyncio.sleep(0.5)

            elif scene_type == "televote":
                embed_staff = generate_scoreboard_embed(poll_id, poll_data, reveal_staff=True, reveal_members=False)
                await text_channel.send("📊 **Standings After Staff Jury Voting:**", embed=embed_staff)
                await asyncio.sleep(0.8)
                
                await text_channel.send("🗳️ **Now Announcing the Public Member Televotes!**")
                await play_audio_file(vc, audio_file)
                
                televote_summary = "\n".join([f"• **{pts} pts** ➡️ {c}" for c, pts in sorted_televotes])
                await text_channel.send(f"📊 **Public Televote Points Added:**\n{televote_summary}")
                await asyncio.sleep(0.8)

            elif scene_type == "winner":
                poll_data["status"] = "closed"
                await text_channel.send("🥁 **AND THE WINNER IS...**")
                await play_audio_file(vc, audio_file)

                embed_final = generate_scoreboard_embed(poll_id, poll_data, reveal_staff=True, reveal_members=True)
                await text_channel.send(f"🏆 **🎉 CONGRATULATIONS TO OUR WINNER: {winner_name}! 🎉**", embed=embed_final)

        await asyncio.sleep(2.5)
        if vc and vc.is_connected():
            await vc.disconnect()

    except Exception as e:
        await text_channel.send(f"❌ An error occurred during the live broadcast: `{e}`")
        print(f"[Live Show Error]: {e}")
        if 'vc' in locals() and vc and vc.is_connected():
            await vc.disconnect()

    finally:
        # Clean up temporary audio files
        for f in generated_audio:
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
