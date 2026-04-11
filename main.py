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
DJ_ROLE_NAME = os.getenv("DJ_ROLE_NAME", "DJ")
SONG_LIMIT = int(os.getenv("SONG_LIMIT_PER_USER", "5"))   # 0 = unlimited
IDLE_TIMEOUT = 300

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
    requested_by_id: int = 0


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
        self.history: deque[Track] = deque(maxlen=10)
        self.reconnect_task: Optional[asyncio.Task] = None


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


def total_queue_duration(q: GuildQueue) -> str:
    secs = sum(t.duration_secs or 0 for t in q.tracks)
    if q.current and q.current.duration_secs:
        secs += q.current.duration_secs
    return format_duration(secs) if secs else "Unknown"


# ---------------------------------------------------------------------------
# Guards helpers
# ---------------------------------------------------------------------------

def has_dj_role(member: discord.Member) -> bool:
    """True if the member has the DJ role, or the DJ role doesn't exist at all."""
    role = discord.utils.get(member.guild.roles, name=DJ_ROLE_NAME)
    if role is None:
        return True   # role not configured — allow everyone
    return role in member.roles


def user_song_count(q: GuildQueue, user_id: int) -> int:
    return sum(1 for t in q.tracks if t.requested_by_id == user_id)


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


def _info_to_track(info: dict, requested_by: str, requested_by_id: int = 0) -> Track:
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
        requested_by_id=requested_by_id,
    )


async def fetch_track(query: str, requested_by: str, requested_by_id: int = 0) -> "Optional[Track | str]":
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

    return _info_to_track(info, requested_by, requested_by_id)


async def fetch_playlist(url: str, requested_by: str, requested_by_id: int = 0) -> list[Track]:
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
            stream_url="",
            duration=format_duration(duration_secs),
            duration_secs=int(duration_secs) if duration_secs else None,
            thumbnail=entry.get("thumbnail", ""),
            requested_by=requested_by,
            requested_by_id=requested_by_id,
        ))
    return tracks


async def search_youtube(query: str, count: int = 5) -> list[dict]:
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
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            return info.get("entries", [])

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _search), timeout=8.0)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _make_source(track: Track, volume: float, seek_secs: int = 0) -> discord.FFmpegOpusAudio:
    before_opts = FFMPEG_BEFORE_OPTS
    if seek_secs > 0:
        before_opts = f"-ss {seek_secs} " + before_opts
    options = "-vn"
    if abs(volume - 1.0) > 0.005:
        options += f" -af volume={volume}"
    return discord.FFmpegOpusAudio(
        track.stream_url,
        before_options=before_opts,
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
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(f"❌ You need the **{DJ_ROLE_NAME}** role to skip.", ephemeral=True)
            return
        q.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q:
            await interaction.response.send_message("❌ Not connected.", ephemeral=True)
            return
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(f"❌ You need the **{DJ_ROLE_NAME}** role.", ephemeral=True)
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
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(f"❌ You need the **{DJ_ROLE_NAME}** role.", ephemeral=True)
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

        total_secs = sum(t.duration_secs or 0 for t in self.q.tracks)
        total_str = format_duration(total_secs) if total_secs else "?"
        embed.set_footer(
            text=f"Page {self.page + 1}/{self._total_pages()} • {len(self.q.tracks)} song(s) • Total: {total_str}"
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
# Search select menu
# ---------------------------------------------------------------------------

class SearchView(discord.ui.View):
    def __init__(self, entries: list[dict], guild: discord.Guild, q: GuildQueue, requested_by: str, requested_by_id: int):
        super().__init__(timeout=30)
        self.entries = entries
        self.guild = guild
        self.q = q
        self.requested_by = requested_by
        self.requested_by_id = requested_by_id

        options = []
        for i, e in enumerate(entries[:5]):
            title = e.get("title", "Unknown")[:80]
            dur = e.get("duration")
            desc = f"{format_duration(dur)}" if dur else "Unknown duration"
            options.append(discord.SelectOption(label=f"{i+1}. {title}", description=desc, value=str(i)))

        select = discord.ui.Select(placeholder="Choose a song to play…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        entry = self.entries[idx]
        vid_id = entry.get("id", "")
        url = f"https://youtu.be/{vid_id}"

        await interaction.response.defer()

        track = await fetch_track(url, self.requested_by, self.requested_by_id)
        if not track or isinstance(track, str):
            await interaction.followup.send("❌ Couldn't load that track.", ephemeral=True)
            return

        if self.q.voice_client and (self.q.voice_client.is_playing() or self.q.voice_client.is_paused() or self.q.current):
            self.q.tracks.append(track)
            embed = discord.Embed(
                title="➕ Added to Queue",
                description=f"**[{track.title}]({track.webpage_url})**",
                color=0x5865F2,
            )
            embed.add_field(name="Duration", value=track.duration, inline=True)
            embed.add_field(name="Position", value=str(len(self.q.tracks)), inline=True)
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            await interaction.followup.send(embed=embed)
        else:
            self.q.current = track
            await _start_playing(self.guild, self.q)
            await interaction.followup.send("▶️ Starting playback…")

        self.stop()


# ---------------------------------------------------------------------------
# Internal playback engine
# ---------------------------------------------------------------------------

async def _resolve_stream(track: Track) -> bool:
    if track.stream_url:
        return True
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            return ydl.extract_info(track.webpage_url, download=False)

    try:
        info = await loop.run_in_executor(None, _extract)
        resolved = _info_to_track(info, track.requested_by, track.requested_by_id)
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


async def _update_presence(bot: commands.Bot, track: Optional[Track]) -> None:
    try:
        if track:
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=track.title,
                )
            )
        else:
            await bot.change_presence(activity=None)
    except Exception:
        pass


async def _start_playing(guild: discord.Guild, q: GuildQueue, seek_secs: int = 0) -> None:
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

    source = _make_source(q.current, q.volume, seek_secs)
    q.play_start = time.monotonic() - seek_secs

    if q.idle_task:
        q.idle_task.cancel()
        q.idle_task = None

    def after_play(error):
        if error:
            print(f"[Player] Error: {error}")
        asyncio.run_coroutine_threadsafe(_play_next(guild), guild._state.loop)

    q.voice_client.play(source, after=after_play)
    print(f"[Player] Now playing: {q.current.title}")

    asyncio.create_task(_update_presence(guild._state.client, q.current))

    if q.text_channel and seek_secs == 0:
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

    if q.current:
        q.history.append(q.current)

    if q.loop_mode == "queue" and q.current:
        q.tracks.append(q.current)

    if not q.tracks:
        q.current = None
        asyncio.create_task(_update_presence(guild._state.client, None))
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


async def _reconnect_monitor(guild: discord.Guild) -> None:
    """Background task: if bot was playing but voice dropped, attempt to reconnect."""
    await asyncio.sleep(15)
    q = queues.get(guild.id)
    if not q or not q.current:
        return
    if q.voice_client and not q.voice_client.is_connected() and not q.voice_client.is_playing():
        print(f"[Reconnect] Voice dropped in {guild.name}, attempting reconnect…")
        try:
            if q.voice_client.channel:
                vc = await q.voice_client.channel.connect(timeout=20.0, reconnect=True)
                q.voice_client = vc
                q.current.stream_url = ""   # force re-resolve since URL likely expired
                await _start_playing(guild, q)
                if q.text_channel:
                    await q.text_channel.send("🔄 Reconnected and resumed playback.")
        except Exception as e:
            print(f"[Reconnect] Failed: {e}")


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

        if before.channel is None and after.channel is not None:
            if not guild.voice_client:
                try:
                    vc = await after.channel.connect()
                    get_queue(guild.id).voice_client = vc
                    print(f"[Auto-join] #{after.channel.name}")
                except Exception as e:
                    print(f"[Auto-join] Failed: {e}")
            return

        if before.channel is not None and after.channel != before.channel:
            vc = guild.voice_client
            if vc and vc.channel == before.channel:
                humans = [m for m in before.channel.members if not m.bot]
                if not humans:
                    await vc.disconnect()
                    queues.pop(guild.id, None)
                    await self.change_presence(activity=None)
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


def dj_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(
                f"❌ You need the **{DJ_ROLE_NAME}** role to use this command.",
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

    user_id = interaction.user.id
    if SONG_LIMIT > 0 and user_song_count(q, user_id) >= SONG_LIMIT:
        await interaction.followup.send(
            f"❌ You already have **{SONG_LIMIT}** songs in the queue. Wait for one to finish.",
            ephemeral=True,
        )
        return

    is_playlist = ("list=" in query) and (query.startswith("http://") or query.startswith("https://"))

    if is_playlist:
        tracks = await fetch_playlist(query, str(interaction.user), user_id)
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

    track = await fetch_track(query, str(interaction.user), user_id)
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


@bot.tree.command(name="search", description="Search YouTube and pick from a list of results")
@app_commands.describe(query="What to search for")
@music_channel_only()
async def cmd_search(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    q = await ensure_voice(interaction)
    if q is None:
        return

    entries = await search_youtube(query, count=5)
    if not entries:
        await interaction.followup.send("❌ No results found. Try a different search.")
        return

    lines = []
    for i, e in enumerate(entries[:5], 1):
        title = e.get("title", "Unknown")
        dur = e.get("duration")
        dur_str = format_duration(dur) if dur else "?"
        vid_id = e.get("id", "")
        url = f"https://youtu.be/{vid_id}" if vid_id else ""
        lines.append(f"`{i}.` [{title}]({url}) `{dur_str}`")

    embed = discord.Embed(
        title=f"🔍 Results for: {query}",
        description="\n".join(lines),
        color=0x5865F2,
    )
    embed.set_footer(text="Select a song from the dropdown below")

    view = SearchView(entries[:5], interaction.guild, q, str(interaction.user), interaction.user.id)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="skip", description="Skip the current song")
@music_channel_only()
@dj_only()
async def cmd_skip(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    q.voice_client.stop()
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(name="skipto", description="Skip to a specific position in the queue")
@app_commands.describe(position="Queue position to jump to")
@music_channel_only()
@dj_only()
async def cmd_skipto(interaction: discord.Interaction, position: int):
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
    skipped = track_list[:position - 1]
    remaining = track_list[position - 1:]

    for t in skipped:
        q.history.append(t)

    q.tracks = deque(remaining)

    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()
        await interaction.response.send_message(f"⏭️ Jumped to position **{position}**.")
    else:
        q.current = q.tracks.popleft()
        await _start_playing(interaction.guild, q)
        await interaction.response.send_message(f"▶️ Playing position **{position}**.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
@music_channel_only()
@dj_only()
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
    await bot.change_presence(activity=None)
    await interaction.response.send_message("⏹️ Stopped and disconnected.")


@bot.tree.command(name="pause", description="Pause the current song")
@music_channel_only()
@dj_only()
async def cmd_pause(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_playing():
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    q.voice_client.pause()
    await interaction.response.send_message("⏸️ Paused.")


@bot.tree.command(name="resume", description="Resume the paused song")
@music_channel_only()
@dj_only()
async def cmd_resume(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_paused():
        await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
        return
    q.voice_client.resume()
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="replay", description="Restart the current song from the beginning")
@music_channel_only()
@dj_only()
async def cmd_replay(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.current:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()

    q.current.stream_url = ""   # force re-resolve
    await _start_playing(interaction.guild, q)
    await interaction.response.send_message("🔄 Restarting from the beginning.")


@bot.tree.command(name="loop", description="Toggle loop mode: off → song → queue")
@music_channel_only()
@dj_only()
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
@dj_only()
async def cmd_shuffle(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
        return
    track_list = list(q.tracks)
    random.shuffle(track_list)
    q.tracks = deque(track_list)
    await interaction.response.send_message(f"🔀 Shuffled **{len(track_list)}** songs.")


@bot.tree.command(name="clear", description="Clear all upcoming songs without stopping the current one")
@music_channel_only()
@dj_only()
async def cmd_clear(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is already empty.", ephemeral=True)
        return
    count = len(q.tracks)
    q.tracks.clear()
    await interaction.response.send_message(f"🗑️ Cleared **{count}** song(s) from the queue.")


@bot.tree.command(name="remove", description="Remove a song from the queue by its position")
@app_commands.describe(position="Position number from /queue")
@music_channel_only()
@dj_only()
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
@dj_only()
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
    await interaction.response.send_message(f"↕️ Moved **{track.title}** to position **{to_pos}**.")


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


@bot.tree.command(name="history", description="Show the last 10 songs that were played")
@music_channel_only()
async def cmd_history(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.history:
        await interaction.response.send_message("📭 No history yet.", ephemeral=True)
        return
    lines = []
    for i, track in enumerate(reversed(list(q.history)), 1):
        lines.append(f"`{i}.` [{track.title}]({track.webpage_url}) `{track.duration}` — {track.requested_by}")
    embed = discord.Embed(title="📜 Recently Played", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="volume", description="Set the volume (1–100)")
@app_commands.describe(level="Volume level between 1 and 100")
@music_channel_only()
@dj_only()
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

    old_volume = q.volume
    q.volume = level / 100

    if (
        q.voice_client
        and q.current
        and (q.voice_client.is_playing() or q.voice_client.is_paused())
        and abs(old_volume - q.volume) > 0.005
    ):
        elapsed = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.voice_client.stop()
        await _start_playing(interaction.guild, q, seek_secs=elapsed)
        await interaction.response.send_message(f"🔊 Volume set to **{level}%** — applied immediately.")
    else:
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")


@bot.tree.command(name="help", description="Show all commands and tips")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="🎵 Different Music — Commands & Tips", color=0x5865F2)

    embed.add_field(
        name="▶️ Playback",
        value=(
            "`/play <song>` — Search YouTube, paste a URL, or a playlist link\n"
            "`/search <query>` — Pick from a list of 5 results\n"
            "`/pause` — Pause the current song\n"
            "`/resume` — Resume playback\n"
            "`/replay` — Restart the current song\n"
            "`/skip` — Skip to the next song\n"
            "`/stop` — Stop and disconnect the bot"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Queue",
        value=(
            "`/queue` — View the queue (paginated with total duration)\n"
            "`/nowplaying` — See what's currently playing\n"
            "`/history` — See the last 10 songs played\n"
            "`/skipto <pos>` — Jump to a position in the queue\n"
            "`/remove <pos>` — Remove a song by position\n"
            "`/move <from> <to>` — Reorder songs\n"
            "`/clear` — Clear the queue (keeps current song)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎛️ Controls",
        value=(
            "`/loop` — Cycle loop mode: Off → Song → Queue\n"
            "`/shuffle` — Shuffle the queue\n"
            "`/volume <1–100>` — Adjust volume (applies immediately)"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Tips",
        value=(
            f"• DJ commands require the **{DJ_ROLE_NAME}** role\n"
            f"• Max {SONG_LIMIT} songs per person in queue (0 = unlimited)\n"
            "• Buttons on Now Playing card: ⏸️ ⏭️ 🔁 🔀\n"
            "• Autocomplete shows results as you type in `/play`\n"
            "• Paste a YouTube playlist URL to queue up to 25 songs\n"
            "• Bot shows current song as its Discord status\n"
            "• Bot auto-leaves after 5 min of inactivity"
        ),
        inline=False,
    )
    embed.set_footer(text="Different Music Bot")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

bot.run(TOKEN)
