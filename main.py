import os
import asyncio
import uuid
import wave
import base64
import subprocess
import traceback
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
TTS_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-preview-tts",
    "gemini-3.1-flash-tts-preview"
]

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


# --- AUDIO SYNTHESIS & BROADCAST MIXING ---

def get_audio_duration(wav_path: str) -> float:
    """Calculates duration in seconds of a WAV file."""
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate)
    except Exception:
        return 4.0

def mix_audio_with_bgm(tts_wav_path: str, bgm_key: str, bgm_volume: float = 0.22) -> str:
    """Seamlessly mixes BGM under speech with a clean music tail and fade-out."""
    bgm_path = BGM_FILES.get(bgm_key)
    if not bgm_path or not os.path.exists(bgm_path):
        return tts_wav_path

    voice_duration = get_audio_duration(tts_wav_path)
    total_duration = max(1.5, voice_duration + 1.2)
    fade_start = max(0.1, total_duration - 1.0)

    mixed_output_path = f"mixed_{uuid.uuid4().hex[:8]}.wav"
    try:
        filter_complex = (
            f"[0:a]volume=1.0[voice];"
            f"[1:a]volume={bgm_volume},afade=t=in:st=0:d=0.3,afade=t=out:st={fade_start:.2f}:d=0.9[music];"
            f"[voice][music]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", tts_wav_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-t", f"{total_duration:.2f}",
            "-c:a", "pcm_s16le",
            mixed_output_path
        ]
        
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
        
        if os.path.exists(tts_wav_path):
            try:
                os.remove(tts_wav_path)
            except Exception:
                pass
            
        return mixed_output_path
    except Exception as e:
        print(f"[Audio Mixing Warning for {bgm_key}]: {e}")
        return tts_wav_path

async def synthesize_achird_speech(text: str, retries: int = 4) -> str:
    """Synthesizes speech using Achird with exponential backoff on rate limits."""
    if not text or not text.strip():
        return None

    if not ai_client:
        raise ValueError("GEMINI_API_KEY environment variable is not configured.")

    clean_text = text.replace("*", "").replace("#", "").strip()
    prompt = f"Please read the following text aloud with the energetic, charismatic, dramatic delivery of a Eurovision game show host. Pause with suspense before revealing the recipient: {clean_text}"

    temp_wav = f"gemini_achird_{uuid.uuid4().hex[:8]}.wav"
    audio_bytes = None
    last_error = None

    for attempt in range(1, retries + 1):
        for model in TTS_MODELS:
            try:
                response = await ai_client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
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
                last_error = e
                print(f"[Gemini TTS {model} (Attempt {attempt})]: {e}")

        if audio_bytes:
            break
        
        # Exponential backoff if rate limited
        backoff_time = 3 * attempt
        print(f"Waiting {backoff_time}s for Gemini API quota recovery...")
        await asyncio.sleep(backoff_time)

    if not audio_bytes:
        raise RuntimeError(f"Gemini TTS Error for: '{clean_text[:35]}...' -> {last_error}")

    with wave.open(temp_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(audio_bytes)

    return temp_wav

async def play_audio_file(voice_client: discord.VoiceClient, filepath: str):
    """Accurately streams audio file to Discord voice without cutting off."""
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


# --- LIVE SHOW CONTROLLER & START BUTTON VIEW ---

class StartBroadcastView(discord.ui.View):
    def __init__(self, scenes, poll_id, poll_data, sorted_televotes, winner_name, voice_channel, text_channel):
        super().__init__(timeout=900)
        self.scenes = scenes
        self.poll_id = poll_id
        self.poll_data = poll_data
        self.sorted_televotes = sorted_televotes
        self.winner_name = winner_name
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.started = False

    @discord.ui.button(label="Start Grand Final Broadcast", style=discord.ButtonStyle.danger, emoji="🔴")
    async def start_show_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        if not member or not (member.id == interaction.guild.owner_id or member.guild_permissions.administrator or any(role.id in (STAFF_ROLE_ID, HR_ROLE_ID) for role in member.roles)):
            await interaction.response.send_message("❌ Only server Staff or HR can launch the live broadcast.", ephemeral=True)
            return

        if self.started:
            await interaction.response.send_message("❌ The broadcast is already running!", ephemeral=True)
            return

        self.started = True
        button.disabled = True
        button.label = "Broadcast In Progress..."
        await interaction.response.edit_message(
            content=f"🎙️ **Broadcasting Live in {self.voice_channel.mention}!**", 
            view=self
        )

        asyncio.create_task(
            execute_broadcast(
                self.scenes, self.poll_id, self.poll_data, 
                self.sorted_televotes, self.winner_name, 
                self.voice_channel, self.text_channel
            )
        )

    async def on_timeout(self):
        if not self.started:
            for sc in self.scenes:
                f = sc.get("audio_file")
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass


async def execute_broadcast(scenes, poll_id, poll_data, sorted_televotes, winner_name, voice_channel, text_channel):
    vc = None
    try:
        try:
            vc = await voice_channel.connect()
        except Exception:
            vc = voice_channel.guild.voice_client

        await text_channel.send("✨ **THE GRAND FINAL BROADCAST IS NOW LIVE!** 🎙️")

        for sc in scenes:
            scene_type = sc["type"]
            audio_file = sc["audio_file"]

            if scene_type == "intro":
                await play_audio_file(vc, audio_file)
                await asyncio.sleep(0.6)

            elif scene_type == "jury":
                await play_audio_file(vc, audio_file)
                jury_info = sc["jury_data"]
                sorted_ballot = sorted(jury_info["ballot"].items(), key=lambda x: x[1], reverse=True)
                ballot_display = "\n".join([f"• **{p} pts** ➡️ {c}" for c, p in sorted_ballot])
                await text_channel.send(f"🎙️ **Jury Ballot from {jury_info['name']}:**\n{ballot_display}")
                await asyncio.sleep(0.6)

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
        await text_channel.send(f"❌ An error occurred during the broadcast: `{e}`")
        print(f"[Live Show Error]: {e}")
        if vc and vc.is_connected():
            await vc.disconnect()

    finally:
        for sc in scenes:
            f = sc.get("audio_file")
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass


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
    
    await interaction.response.send_message(f"⏳ **Preparing all broadcast scenes with Achird voice... Please wait.**")
    progress_msg = await interaction.original_response()

    # --- 1. CALCULATE STANDINGS ---
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

    # --- 2. BUILD STRUCTURED SCENES (SINGLE-CALL SUSPENSE SCRIPT) ---
    scenes = []

    # Scene 1: Intro
    scenes.append({
        "type": "intro",
        "bgm": "intro",
        "script": (
            f"Good evening Europe, good evening world, and welcome to the Grand Final of {poll_data['title']}! "
            f"The atmosphere in the arena is electric. Tonight, one of our amazing nominees will be crowned champion. "
            f"Let the Eurovision voting begin!"
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
                f"Thank you for your service, {name}. The tension is building in the arena. "
                f"And {name}'s coveted twelve points go to... {twelve_pts_candidate}!"
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

    # Scene N+2: Grand Winner Coronation
    scenes.append({
        "type": "winner",
        "bgm": "winner",
        "script": (
            f"Ladies and gentlemen, the moment of truth has finally arrived! "
            f"The final points have been tallied. "
            f"With an unbelievable total of {winner_points} points, the champion and winner of {poll_data['title']} is... {winner_name}! "
            f"A massive congratulations to {winner_name}! Thank you all for an unforgettable night, and goodnight!"
        )
    })

    # --- 3. PRE-SYNTHESIZE ALL SCENES (1 CALL PER SCENE + BACKOFF) ---
    try:
        for idx, sc in enumerate(scenes, 1):
            await progress_msg.edit(content=f"🎙️ **Synthesizing Scene {idx}/{len(scenes)} with Achird voice & mixing BGM...**")
            speech_file = await synthesize_achird_speech(sc["script"])
            mixed_file = await asyncio.to_thread(mix_audio_with_bgm, speech_file, sc["bgm"])
            sc["audio_file"] = mixed_file
            
            # Pacing delay to stay well under Gemini RPM limits
            await asyncio.sleep(1.2)

        # --- 4. SHOW INTERACTIVE START BUTTON ---
        start_view = StartBroadcastView(
            scenes=scenes,
            poll_id=poll_id,
            poll_data=poll_data,
            sorted_televotes=sorted_televotes,
            winner_name=winner_name,
            voice_channel=voice_channel,
            text_channel=interaction.channel
        )

        await progress_msg.edit(
            content=(
                f"✅ **All {len(scenes)} Broadcast Scenes Are Ready & Staged!**\n\n"
                f"🔊 **Target Voice Channel:** {voice_channel.mention}\n"
                f"🎙️ **Voice Host:** Achird (Gemini AI)\n"
                f"✨ Press the button below whenever you are ready to start the live show."
            ),
            view=start_view
        )

    except Exception as e:
        print(f"[Preparation Error]: {e}")
        traceback.print_exc()
        await progress_msg.edit(
            content=f"❌ **Failed to prepare broadcast scenes:** `{e}`\nPlease verify your `GEMINI_API_KEY` and try again."
        )


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
