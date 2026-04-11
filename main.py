import asyncio
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MUSIC_CHANNEL_ID = int(os.getenv("MUSIC_CHANNEL_ID", "1487195424111726743"))
IDLE_TIMEOUT = 300  # seconds of silence before auto-leave

if not TOKEN:
    raise ValueError("DISCORD_TOKEN is not set")

# ---------------------------------------------------------------------------
# Audio config
# ---------------------------------------------------------------------------

FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-thread_queue_size 512"
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: str
    duration_secs: Optional[int]
    thumbnail: str
    requested_by: str


class GuildQueue:
    def __init__(self):
        self.tracks: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.volume: float = 1.0
        self.loop_mode: str = "off"   # "off" | "song" | "queue"
        self.play_start: Optional[float] = None
        self.idle_task: Optional[asyncio.Task] = None


queues: dict[int, GuildQueue] = {}


def get_queue(guild_id: int) -> GuildQueue:
    if guild_id not in queues:
        queues[guild_id] = GuildQueue()
    return queues[guild_id]


def format_duration(seconds) -> str:
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

YDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[protocol=https]/bestaudio",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "noplaylist": True,
}

UNSUPPORTED_DOMAINS = (
    "music.apple.com", "spotify.com", "open.spotify.com",
    "tidal.com", "deezer.com",
)


def _info_to_track(info: dict, requested_by: str) -> Track:
    thumbs = info.get("thumbnails") or []
    thumbnail = thumbs[-1]["url"] if thumbs else info.get("thumbnail", "")
    stream_url = info.get("url", "")
    if not stream_url:
        for fmt in reversed(info.get("formats", [])):
            if fmt.get("acodec") != "none" and fmt.get("url"):
                stream_url = fmt["url"]
                break
    duration_secs = info.get("duration")
    return Track(
        title=info.get("title", "Unknown"),
        webpage_url=info.get("webpage_url", ""),
        stream_url=stream_url,
        duration=format_duration(duration_secs),
        duration_secs=int(duration_secs) if duration_secs else None,
        thumbnail=thumbnail,
        requested_by=requested_by,
    )


async def fetch_track(query: str, requested_by: str) -> "Optional[Track | str]":
    is_url = query.startswith("http://") or query.startswith("https://")

    if is_url and any(d in query for d in UNSUPPORTED_DOMAINS):
        return (
            "❌ That link isn't supported. Paste a **YouTube URL** or search by name instead.\n"
            "Example: `/play Gunna Different Species`"
        )

    target = query if is_url else f"ytsearch1:{query}"
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(target, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    try:
        info = await loop.run_in_executor(None, _extract)
    except Exception as e:
        print(f"[yt-dlp] Error: {e}")
        return None

    return _info_to_track(info, requested_by)


async def fetch_playlist(url: str, requested_by: str) -> list[Track]:
    """Fetch up to 25 tracks from a YouTube playlist (flat, fast)."""
    loop = asyncio.get_event_loop()

    def _extract():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": False,
            "playlistend": 25,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("entries", [])

    try:
        entries = await loop.run_in_executor(None, _extract)
    except Exception as e:
        print(f"[yt-dlp Playlist] Error: {e}")
        return []

    tracks = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        duration_secs = entry.get("duration")
        tracks.append(Track(
            title=entry.get("title", "Unknown"),
            webpage_url=f"https://youtu.be/{entry['id']}",
            stream_url="",  # resolved lazily when the track is about to play
            duration=format_duration(duration_secs),
            duration_secs=int(duration_secs) if duration_secs else None,
            thumbnail=entry.get("thumbnail", ""),
            requested_by=requested_by,
        ))
    return tracks


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _make_source(track: Track, volume: float) -> discord.FFmpegOpusAudio:
    # Only apply volume filter when not at default — avoids unnecessary CPU
    # transcoding on the Raspberry Pi which causes audio stuttering.
    options = "-vn"
    if abs(volume - 1.0) > 0.005:
        options += f" -af volume={volume}"
    return discord.FFmpegOpusAudio(
        track.stream_url,
        before_options=FFMPEG_BEFORE_OPTS,
        options=options,
    )


LOOP_LABELS = {"off": "Off", "song": "🔂 Song", "queue": "🔁 Queue"}


def _now_playing_embed(track: Track, loop_mode: str = "off", play_start: Optional[float] = None) -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=0x5865F2,
    )
    embed.add_field(name="Duration", value=track.duration, inline=True)
    embed.add_field(name="Requested by", value=track.requested_by, inline=True)
    embed.add_field(name="Loop", value=LOOP_LABELS.get(loop_mode, "Off"), inline=True)

    if play_start and track.duration_secs:
        elapsed = int(time.monotonic() - play_start)
        remaining = track.duration_secs - elapsed
        if 0 <= remaining <= track.duration_secs:
            embed.add_field(name="Time Remaining", value=format_duration(remaining), inline=True)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


# ---------------------------------------------------------------------------
# Interactive buttons
# ---------------------------------------------------------------------------

class NowPlayingView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def _q(self) -> Optional[GuildQueue]:
        return queues.get(self.guild_id)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="np_pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.voice_client:
            await interaction.response.send_message("❌ Not connected.", ephemeral=True)
            return
        if q.voice_client.is_playing():
            q.voice_client.pause()
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif q.voice_client.is_paused():
            q.voice_client.resume()
            button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return
        q.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q:
            await interaction.response.send_message("❌ Not connected.", ephemeral=True)
            return
        modes = ["off", "song", "queue"]
        q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % len(modes)]
        labels = {"off": "🔁 Loop: Off", "song": "🔂 Loop: Song", "queue": "🔁 Loop: Queue"}
        await interaction.response.send_message(f"✅ {labels[q.loop_mode]}", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="np_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.tracks:
            await interaction.response.send_message("❌ Queue is empty.", ephemeral=True)
            return
        track_list = list(q.tracks)
        random.shuffle(track_list)
        q.tracks = deque(track_list)
        await interaction.response.send_message("🔀 Queue shuffled!", ephemeral=True)


# ---------------------------------------------------------------------------
# Queue pagination view
# ---------------------------------------------------------------------------

TRACKS_PER_PAGE = 10


class QueueView(discord.ui.View):
    def __init__(self, q: GuildQueue):
        super().__init__(timeout=60)
        self.q = q
        self.page = 0

    def _total_pages(self) -> int:
        return max(1, (len(self.q.tracks) + TRACKS_PER_PAGE - 1) // TRACKS_PER_PAGE)

    def _build_embed(self) -> discord.Embed:
        lines = []
        if self.q.current and self.page == 0:
            lines.append(
                f"**▶️ Now Playing:** [{self.q.current.title}]({self.q.current.webpage_url}) "
                f"`{self.q.current.duration}` — {self.q.current.requested_by}"
            )

        track_list = list(self.q.tracks)
        start = self.page * TRACKS_PER_PAGE
        for i, track in enumerate(track_list[start:start + TRACKS_PER_PAGE], start + 1):
            lines.append(
                f"`{i}.` [{track.title}]({track.webpage_url}) `{track.duration}` — {track.requested_by}"
            )

        embed = discord.Embed(
            title="🎵 Queue",
            description="\n".join(lines) if lines else "The queue is empty.",
            color=0x5865F2,
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self._total_pages()} • {len(self.q.tracks)} song(s) in queue"
        )
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self._total_pages() - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


# ---------------------------------------------------------------------------
# Internal playback engine
# ---------------------------------------------------------------------------

async def _resolve_stream(track: Track) -> bool:
    """If the track has no stream_url (playlist track), fetch it now."""
    if track.stream_url:
        return True
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            return ydl.extract_info(track.webpage_url, download=False)

    try:
        info = await loop.run_in_executor(None, _extract)
        resolved = _info_to_track(info, track.requested_by)
        track.stream_url = resolved.stream_url
        track.title = resolved.title
        track.duration = resolved.duration
        track.duration_secs = resolved.duration_secs
        track.thumbnail = resolved.thumbnail
        track.webpage_url = resolved.webpage_url
        return bool(track.stream_url)
    except Exception as e:
        print(f"[Resolve] Failed for {track.webpage_url}: {e}")
        return False


async def _start_playing(guild: discord.Guild, q: GuildQueue) -> None:
    if not q.voice_client or q.current is None:
        return
    if not q.voice_client.is_connected():
        print("[Player] Voice client not connected, skipping playback")
        q.current = None
        return

    if not await _resolve_stream(q.current):
        print(f"[Player] Could not resolve stream for {q.current.title}, skipping")
        await _play_next(guild)
        return

    source = _make_source(q.current, q.volume)
    q.play_start = time.monotonic()

    if q.idle_task:
        q.idle_task.cancel()
        q.idle_task = None

    def after_play(error):
        if error:
            print(f"[Player] Error: {error}")
        asyncio.run_coroutine_threadsafe(_play_next(guild), guild._state.loop)

    q.voice_client.play(source, after=after_play)
    print(f"[Player] Now playing: {q.current.title}")

    if q.text_channel:
        view = NowPlayingView(guild.id)
        await q.text_channel.send(
            embed=_now_playing_embed(q.current, q.loop_mode, q.play_start),
            view=view,
        )


async def _play_next(guild: discord.Guild) -> None:
    q = queues.get(guild.id)
    if q is None:
        return

    if q.loop_mode == "song" and q.current:
        await _start_playing(guild, q)
        return

    if q.loop_mode == "queue" and q.current:
        q.tracks.append(q.current)

    if not q.tracks:
        q.current = None
        if q.voice_client and q.voice_client.is_connected():
            if q.idle_task:
                q.idle_task.cancel()
            q.idle_task = asyncio.create_task(_idle_disconnect(guild))
        return

    q.current = q.tracks.popleft()
    await _start_playing(guild, q)


async def _idle_disconnect(guild: discord.Guild) -> None:
    await asyncio.sleep(IDLE_TIMEOUT)
    q = queues.get(guild.id)
    if q and q.voice_client and q.voice_client.is_connected() and not q.voice_client.is_playing():
        channel_name = q.voice_client.channel.name if q.voice_client.channel else "unknown"
        await q.voice_client.disconnect()
        queues.pop(guild.id, None)
        print(f"[Auto-leave] Idle timeout in #{channel_name}")
        if q.text_channel:
            try:
                await q.text_channel.send("💤 Left the voice channel due to inactivity.")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("📡 Slash commands synced globally")

    async def on_ready(self):
        print(f"🤖 Logged in as {self.user}")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        guild = member.guild

        # Auto-join: first human enters a voice channel
        if before.channel is None and after.channel is not None:
            if not guild.voice_client:
                try:
                    vc = await after.channel.connect()
                    get_queue(guild.id).voice_client = vc
                    print(f"[Auto-join] #{after.channel.name}")
                except Exception as e:
                    print(f"[Auto-join] Failed: {e}")
            return

        # Auto-leave: bot's channel became empty
        if before.channel is not None and after.channel != before.channel:
            vc = guild.voice_client
            if vc and vc.channel == before.channel:
                humans = [m for m in before.channel.members if not m.bot]
                if not humans:
                    await vc.disconnect()
                    queues.pop(guild.id, None)
                    print(f"[Auto-leave] #{before.channel.name} is empty")


bot = MusicBot()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def music_channel_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != MUSIC_CHANNEL_ID:
            await interaction.response.send_message(
                f"⛔ Music commands only work in <#{MUSIC_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


async def ensure_voice(interaction: discord.Interaction) -> Optional[GuildQueue]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "❌ You need to be in a voice channel first.", ephemeral=True
        )
        return None

    q = get_queue(interaction.guild_id)
    channel = interaction.user.voice.channel

    existing_vc = interaction.guild.voice_client
    if not existing_vc or not existing_vc.is_connected():
        if existing_vc:
            try:
                await existing_vc.disconnect(force=True)
            except Exception:
                pass
        try:
            vc = await channel.connect(timeout=30.0, reconnect=True)
            q.voice_client = vc
        except Exception as e:
            print(f"[Voice] Failed to connect: {e}")
            await interaction.followup.send(
                "❌ Couldn't connect to your voice channel. Try again in a moment.",
                ephemeral=True,
            )
            return None
    else:
        q.voice_client = existing_vc

    q.text_channel = interaction.channel
    return q


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

async def _search_suggestions(current: str) -> list[app_commands.Choice[str]]:
    loop = asyncio.get_event_loop()

    def _search():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{current}", download=False)
            return info.get("entries", [])

    try:
        entries = await asyncio.wait_for(loop.run_in_executor(None, _search), timeout=2.5)
    except Exception:
        return []

    choices = []
    for entry in entries[:10]:
        title = entry.get("title", "Unknown")
        vid_id = entry.get("id", "")
        if not vid_id:
            continue
        url = f"https://youtu.be/{vid_id}"
        duration = entry.get("duration")
        if duration:
            mins, secs = divmod(int(duration), 60)
            label = f"{title} ({mins}:{secs:02d})"
        else:
            label = title
        choices.append(app_commands.Choice(name=label[:100], value=url[:100]))

    return choices


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="play", description="Play a song, search YouTube, or paste a playlist URL")
@app_commands.describe(query="Song name, YouTube URL, or playlist URL")
@music_channel_only()
async def cmd_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    q = await ensure_voice(interaction)
    if q is None:
        return

    # Playlist detection
    is_playlist = ("list=" in query) and (query.startswith("http://") or query.startswith("https://"))

    if is_playlist:
        tracks = await fetch_playlist(query, str(interaction.user))
        if not tracks:
            await interaction.followup.send("❌ Couldn't load that playlist. Check the URL and try again.")
            return

        start_now = q.current is None and not q.voice_client.is_playing()
        for track in tracks:
            q.tracks.append(track)

        if start_now and q.tracks:
            q.current = q.tracks.popleft()
            await _start_playing(interaction.guild, q)

        await interaction.followup.send(
            f"{'▶️ Starting' if start_now else '➕ Added'} **{len(tracks)} songs** from the playlist to the queue."
        )
        return

    # Single track
    track = await fetch_track(query, str(interaction.user))
    if track is None:
        await interaction.followup.send("❌ Could not find that song. Try a different search.")
        return
    if isinstance(track, str):
        await interaction.followup.send(track)
        return

    if q.voice_client.is_playing() or q.voice_client.is_paused() or q.current is not None:
        q.tracks.append(track)
        embed = discord.Embed(
            title="➕ Added to Queue",
            description=f"**[{track.title}]({track.webpage_url})**",
            color=0x5865F2,
        )
        embed.add_field(name="Duration", value=track.duration, inline=True)
        embed.add_field(name="Position", value=str(len(q.tracks)), inline=True)
        embed.add_field(name="Requested by", value=track.requested_by, inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await interaction.followup.send(embed=embed)
    else:
        q.current = track
        await _start_playing(interaction.guild, q)
        await interaction.followup.send("▶️ Starting playback…")


@cmd_play.autocomplete("query")
async def play_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if not current or len(current) < 2:
        return []
    return await _search_suggestions(current)


@bot.tree.command(name="skip", description="Skip the current song")
@music_channel_only()
async def cmd_skip(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    q.voice_client.stop()
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
@music_channel_only()
async def cmd_stop(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True)
        return
    if q.idle_task:
        q.idle_task.cancel()
    q.tracks.clear()
    q.current = None
    q.voice_client.stop()
    await q.voice_client.disconnect()
    queues.pop(interaction.guild_id, None)
    await interaction.response.send_message("⏹️ Stopped and disconnected.")


@bot.tree.command(name="pause", description="Pause the current song")
@music_channel_only()
async def cmd_pause(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_playing():
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    q.voice_client.pause()
    await interaction.response.send_message("⏸️ Paused.")


@bot.tree.command(name="resume", description="Resume the paused song")
@music_channel_only()
async def cmd_resume(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_paused():
        await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
        return
    q.voice_client.resume()
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="loop", description="Toggle loop mode: off → song → queue")
@music_channel_only()
async def cmd_loop(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    modes = ["off", "song", "queue"]
    q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % len(modes)]
    labels = {
        "off": "🔁 Loop is now **Off**",
        "song": "🔂 Now looping the **current song**",
        "queue": "🔁 Now looping the **entire queue**",
    }
    await interaction.response.send_message(labels[q.loop_mode])


@bot.tree.command(name="shuffle", description="Shuffle the upcoming queue")
@music_channel_only()
async def cmd_shuffle(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
        return
    track_list = list(q.tracks)
    random.shuffle(track_list)
    q.tracks = deque(track_list)
    await interaction.response.send_message(f"🔀 Shuffled **{len(track_list)}** songs.")


@bot.tree.command(name="remove", description="Remove a song from the queue by its position")
@app_commands.describe(position="Position number from /queue")
@music_channel_only()
async def cmd_remove(interaction: discord.Interaction, position: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
        return
    if position < 1 or position > len(q.tracks):
        await interaction.response.send_message(
            f"❌ Invalid position. Queue has {len(q.tracks)} song(s).", ephemeral=True
        )
        return
    track_list = list(q.tracks)
    removed = track_list.pop(position - 1)
    q.tracks = deque(track_list)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from the queue.")


@bot.tree.command(name="move", description="Move a song to a different position in the queue")
@app_commands.describe(from_pos="Current position", to_pos="New position")
@music_channel_only()
async def cmd_move(interaction: discord.Interaction, from_pos: int, to_pos: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
        return
    n = len(q.tracks)
    if not (1 <= from_pos <= n) or not (1 <= to_pos <= n):
        await interaction.response.send_message(
            f"❌ Positions must be between 1 and {n}.", ephemeral=True
        )
        return
    track_list = list(q.tracks)
    track = track_list.pop(from_pos - 1)
    track_list.insert(to_pos - 1, track)
    q.tracks = deque(track_list)
    await interaction.response.send_message(
        f"↕️ Moved **{track.title}** to position **{to_pos}**."
    )


@bot.tree.command(name="queue", description="Show the current queue")
@music_channel_only()
async def cmd_queue(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or (q.current is None and not q.tracks):
        await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
        return
    view = QueueView(q)
    await interaction.response.send_message(embed=view._build_embed(), view=view)


@bot.tree.command(name="nowplaying", description="Show what's currently playing")
@music_channel_only()
async def cmd_nowplaying(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or q.current is None:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    view = NowPlayingView(interaction.guild_id)
    await interaction.response.send_message(
        embed=_now_playing_embed(q.current, q.loop_mode, q.play_start),
        view=view,
    )


@bot.tree.command(name="volume", description="Set the volume (1–100)")
@app_commands.describe(level="Volume level between 1 and 100")
@music_channel_only()
async def cmd_volume(interaction: discord.Interaction, level: int):
    if not 1 <= level <= 100:
        await interaction.response.send_message(
            "❌ Volume must be between 1 and 100.", ephemeral=True
        )
        return
    q = queues.get(interaction.guild_id)
    if not q:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True)
        return
    q.volume = level / 100
    await interaction.response.send_message(
        f"🔊 Volume set to **{level}%**. Takes effect on the next track."
    )


@bot.tree.command(name="help", description="Show all commands and tips")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="🎵 Different Music — Commands & Tips", color=0x5865F2)

    embed.add_field(
        name="▶️ Playback",
        value=(
            "`/play <song>` — Search YouTube, paste a URL, or a playlist link\n"
            "`/pause` — Pause the current song\n"
            "`/resume` — Resume playback\n"
            "`/skip` — Skip to the next song\n"
            "`/stop` — Stop and disconnect the bot"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Queue",
        value=(
            "`/queue` — View the queue (paginated)\n"
            "`/nowplaying` — See what's currently playing\n"
            "`/remove <pos>` — Remove a song by position\n"
            "`/move <from> <to>` — Reorder songs in the queue"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎛️ Playback Controls",
        value=(
            "`/loop` — Cycle loop mode: Off → Song → Queue\n"
            "`/shuffle` — Shuffle the upcoming queue\n"
            "`/volume <1–100>` — Adjust the volume"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Tips",
        value=(
            "• Buttons on the **Now Playing** card: ⏸️ ⏭️ 🔁 🔀\n"
            "• Autocomplete: start typing a song name to see suggestions\n"
            "• Paste a YouTube **playlist URL** to queue up to 25 songs\n"
            "• Apple Music / Spotify links **won't work** — search by name\n"
            "• Bot auto-joins when you enter a voice channel\n"
            "• Bot auto-leaves when everyone leaves or after 5 min idle\n"
            "• Music commands only work in this channel"
        ),
        inline=False,
    )
    embed.set_footer(text="Different Music Bot")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

bot.run(TOKEN)
