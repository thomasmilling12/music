import asyncio
import json
import math
import os
import random
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

TOKEN            = os.getenv("DISCORD_TOKEN")
MUSIC_CHANNEL_ID = int(os.getenv("MUSIC_CHANNEL_ID", "1487195424111726743"))
GUILD_ID         = 850386896509337710
DJ_ROLE_NAME     = os.getenv("DJ_ROLE_NAME", "DJ")
SONG_LIMIT       = int(os.getenv("SONG_LIMIT_PER_USER", "5"))   # 0 = unlimited
IDLE_TIMEOUT     = 300
BOT_START        = time.monotonic()

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
    title:          str
    webpage_url:    str
    stream_url:     str
    duration:       str
    duration_secs:  Optional[int]
    thumbnail:      str
    requested_by:   str
    requested_by_id: int = 0


@dataclass
class GuildQueue:
    tracks:            deque       = field(default_factory=deque)
    current:           Optional[Track] = None
    voice_client:      Optional[discord.VoiceClient] = None
    text_channel:      Optional[discord.TextChannel] = None
    volume:            float = 1.0
    loop_mode:         str = "off"   # "off" | "song" | "queue"
    play_start:        Optional[float] = None
    idle_task:         Optional[asyncio.Task] = None
    history:           deque = field(default_factory=lambda: deque(maxlen=10))
    songs_played:      int = 0
    # Flags
    mode_247:          bool = False
    announce:          bool = True
    bass_boost:        bool = False
    # Seek/restart helpers
    restart_current:   bool = False
    seek_to:           int  = 0
    # Vote-skip state
    vote_skip_users:   set  = field(default_factory=set)
    # Live now-playing update
    np_message:        Optional[discord.Message] = None
    np_update_task:    Optional[asyncio.Task] = None


queues: dict[int, GuildQueue] = {}


def get_queue(guild_id: int) -> GuildQueue:
    if guild_id not in queues:
        queues[guild_id] = GuildQueue()
    return queues[guild_id]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_duration(seconds) -> str:
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(elapsed: int, total: int, width: int = 14) -> str:
    if not total:
        return ""
    ratio  = min(elapsed / total, 1.0)
    filled = round(ratio * width)
    bar    = "▓" * filled + "░" * (width - filled)
    return f"`{bar}` {format_duration(elapsed)} / {format_duration(total)}"


def parse_time(s: str) -> Optional[int]:
    """Parse '1:30' or '90' into seconds."""
    try:
        if ":" in s:
            parts = s.strip().split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return int(s)
    except (ValueError, IndexError):
        return None


def queue_total_duration(q: GuildQueue) -> str:
    secs = sum(t.duration_secs or 0 for t in q.tracks)
    return format_duration(secs) if secs else "?"


# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------

def has_dj_role(member: discord.Member) -> bool:
    role = discord.utils.get(member.guild.roles, name=DJ_ROLE_NAME)
    if role is None:
        return True
    return role in member.roles


def user_song_count(q: GuildQueue, user_id: int) -> int:
    return sum(1 for t in q.tracks if t.requested_by_id == user_id)


# ---------------------------------------------------------------------------
# Spotify helper (no API key — uses public oEmbed)
# ---------------------------------------------------------------------------

SPOTIFY_DOMAINS = ("open.spotify.com",)


def _is_spotify(url: str) -> bool:
    return any(d in url for d in SPOTIFY_DOMAINS)


async def _spotify_title(url: str) -> Optional[str]:
    """Return '<track> by <artist>' from Spotify oEmbed, or None on failure."""
    loop = asyncio.get_event_loop()

    def _fetch():
        req = urllib.request.Request(
            f"https://open.spotify.com/oembed?url={url}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read())

    try:
        data = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=8)
        return data.get("title")   # e.g. "Paper Bag Boy - Gunna"
    except Exception:
        return None


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
    "music.apple.com", "tidal.com", "deezer.com",
)


def _info_to_track(info: dict, requested_by: str, requested_by_id: int = 0) -> Track:
    thumbs     = info.get("thumbnails") or []
    thumbnail  = thumbs[-1]["url"] if thumbs else info.get("thumbnail", "")
    stream_url = info.get("url", "")
    if not stream_url:
        for fmt in reversed(info.get("formats", [])):
            if fmt.get("acodec") != "none" and fmt.get("url"):
                stream_url = fmt["url"]
                break
    duration_secs = info.get("duration")
    return Track(
        title         = info.get("title", "Unknown"),
        webpage_url   = info.get("webpage_url", ""),
        stream_url    = stream_url,
        duration      = format_duration(duration_secs),
        duration_secs = int(duration_secs) if duration_secs else None,
        thumbnail     = thumbnail,
        requested_by  = requested_by,
        requested_by_id = requested_by_id,
    )


async def fetch_track(query: str, requested_by: str, requested_by_id: int = 0) -> "Optional[Track | str]":
    is_url = query.startswith("http://") or query.startswith("https://")

    if is_url and any(d in query for d in UNSUPPORTED_DOMAINS):
        return "❌ That link isn't supported. Paste a **YouTube URL** or search by name instead."

    # Convert Spotify URLs → YouTube search
    if is_url and _is_spotify(query):
        title = await _spotify_title(query)
        if not title:
            return "❌ Couldn't read that Spotify link. Try searching by song name instead."
        query  = title
        is_url = False

    target = query if is_url else f"ytsearch1:{query}"
    loop   = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(target, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    try:
        info = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=20)
    except asyncio.TimeoutError:
        print(f"[yt-dlp] Timed out fetching: {target}")
        return None
    except Exception as e:
        print(f"[yt-dlp] Error: {e}")
        return None

    return _info_to_track(info, requested_by, requested_by_id)


async def fetch_playlist(url: str, requested_by: str, requested_by_id: int = 0) -> list[Track]:
    loop = asyncio.get_event_loop()

    def _extract():
        opts = {
            "quiet": True, "no_warnings": True,
            "extract_flat": True, "noplaylist": False, "playlistend": 25,
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
        dur = entry.get("duration")
        tracks.append(Track(
            title          = entry.get("title", "Unknown"),
            webpage_url    = f"https://youtu.be/{entry['id']}",
            stream_url     = "",
            duration       = format_duration(dur),
            duration_secs  = int(dur) if dur else None,
            thumbnail      = entry.get("thumbnail", ""),
            requested_by   = requested_by,
            requested_by_id = requested_by_id,
        ))
    return tracks


async def search_youtube(query: str, count: int = 5) -> list[dict]:
    loop = asyncio.get_event_loop()

    def _search():
        opts = {
            "quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True, "noplaylist": True,
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

def _make_source(track: Track, volume: float, seek_secs: int = 0, bass: bool = False) -> discord.FFmpegOpusAudio:
    before_opts = FFMPEG_BEFORE_OPTS
    if seek_secs > 0:
        before_opts = f"-ss {seek_secs} " + before_opts

    filters = []
    if abs(volume - 1.0) > 0.005:
        filters.append(f"volume={volume}")
    if bass:
        filters.append("bass=g=6")

    options = "-vn"
    if filters:
        options += f" -af {','.join(filters)}"

    return discord.FFmpegOpusAudio(
        track.stream_url,
        before_options=before_opts,
        options=options,
    )


LOOP_LABELS = {"off": "Off", "song": "🔂 Song", "queue": "🔁 Queue"}


def _now_playing_embed(track: Track, loop_mode: str = "off",
                        play_start: Optional[float] = None,
                        bass: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title       = "🎵 Now Playing",
        description = f"**[{track.title}]({track.webpage_url})**",
        color       = 0x5865F2,
    )

    if play_start and track.duration_secs:
        elapsed   = int(time.monotonic() - play_start)
        elapsed   = max(0, min(elapsed, track.duration_secs))
        remaining = track.duration_secs - elapsed
        embed.add_field(name="Progress", value=progress_bar(elapsed, track.duration_secs), inline=False)
        embed.add_field(name="Remaining",    value=format_duration(remaining), inline=True)
    else:
        embed.add_field(name="Duration", value=track.duration, inline=True)

    embed.add_field(name="Requested by", value=track.requested_by, inline=True)
    loop_val = LOOP_LABELS.get(loop_mode, "Off")
    if bass:
        loop_val += "  🔊 Bass"
    embed.add_field(name="Loop", value=loop_val, inline=True)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


# ---------------------------------------------------------------------------
# Live now-playing updater
# ---------------------------------------------------------------------------

async def _np_updater(guild_id: int, track: Track) -> None:
    """Edits the now-playing message every 20 s with a refreshed progress bar."""
    await asyncio.sleep(20)
    while True:
        q = queues.get(guild_id)
        if not q or q.current is not track or not q.np_message:
            return
        try:
            await q.np_message.edit(
                embed=_now_playing_embed(track, q.loop_mode, q.play_start, q.bass_boost)
            )
        except Exception:
            return
        await asyncio.sleep(20)


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
            await interaction.response.send_message("❌ Not connected.", ephemeral=True); return
        if q.voice_client.is_playing():
            q.voice_client.pause(); button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif q.voice_client.is_paused():
            q.voice_client.resume(); button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(f"❌ You need the **{DJ_ROLE_NAME}** role to skip.", ephemeral=True); return
        q.vote_skip_users.clear()
        q.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q:
            await interaction.response.send_message("❌ Not connected.", ephemeral=True); return
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(f"❌ You need the **{DJ_ROLE_NAME}** role.", ephemeral=True); return
        modes = ["off", "song", "queue"]
        q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % len(modes)]
        labels = {"off": "🔁 Loop: Off", "song": "🔂 Loop: Song", "queue": "🔁 Loop: Queue"}
        await interaction.response.send_message(f"✅ {labels[q.loop_mode]}", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="np_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.tracks:
            await interaction.response.send_message("❌ Queue is empty.", ephemeral=True); return
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(f"❌ You need the **{DJ_ROLE_NAME}** role.", ephemeral=True); return
        lst = list(q.tracks); random.shuffle(lst); q.tracks = deque(lst)
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
        return max(1, math.ceil(len(self.q.tracks) / TRACKS_PER_PAGE))

    def _build_embed(self) -> discord.Embed:
        lines = []
        if self.q.current and self.page == 0:
            lines.append(
                f"**▶️ Now Playing:** [{self.q.current.title}]({self.q.current.webpage_url}) "
                f"`{self.q.current.duration}` — {self.q.current.requested_by}"
            )
        lst   = list(self.q.tracks)
        start = self.page * TRACKS_PER_PAGE
        for i, t in enumerate(lst[start:start + TRACKS_PER_PAGE], start + 1):
            lines.append(f"`{i}.` [{t.title}]({t.webpage_url}) `{t.duration}` — {t.requested_by}")

        embed = discord.Embed(
            title       = "🎵 Queue",
            description = "\n".join(lines) if lines else "The queue is empty.",
            color       = 0x5865F2,
        )
        total_secs = sum(t.duration_secs or 0 for t in self.q.tracks)
        total_str  = format_duration(total_secs) if total_secs else "?"
        embed.set_footer(text=f"Page {self.page+1}/{self._total_pages()} • {len(self.q.tracks)} song(s) • Total: {total_str}")
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
    def __init__(self, entries, guild, q, requested_by, requested_by_id):
        super().__init__(timeout=30)
        self.entries        = entries
        self.guild          = guild
        self.q              = q
        self.requested_by   = requested_by
        self.requested_by_id = requested_by_id

        options = []
        for i, e in enumerate(entries[:5]):
            title = e.get("title", "Unknown")[:80]
            dur   = e.get("duration")
            desc  = format_duration(dur) if dur else "Unknown duration"
            options.append(discord.SelectOption(label=f"{i+1}. {title}", description=desc, value=str(i)))

        sel          = discord.ui.Select(placeholder="Choose a song to play…", options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        idx   = int(interaction.data["values"][0])
        entry = self.entries[idx]
        url   = f"https://youtu.be/{entry.get('id', '')}"

        await interaction.response.defer()
        track = await fetch_track(url, self.requested_by, self.requested_by_id)
        if not track or isinstance(track, str):
            await interaction.followup.send("❌ Couldn't load that track.", ephemeral=True); return

        if self.q.voice_client and (self.q.voice_client.is_playing() or self.q.voice_client.is_paused() or self.q.current):
            self.q.tracks.append(track)
            embed = discord.Embed(title="➕ Added to Queue",
                                  description=f"**[{track.title}]({track.webpage_url})**",
                                  color=0x5865F2)
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
        info     = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=20)
        resolved = _info_to_track(info, track.requested_by, track.requested_by_id)
        track.stream_url    = resolved.stream_url
        track.title         = resolved.title
        track.duration      = resolved.duration
        track.duration_secs = resolved.duration_secs
        track.thumbnail     = resolved.thumbnail
        track.webpage_url   = resolved.webpage_url
        return bool(track.stream_url)
    except asyncio.TimeoutError:
        print(f"[Resolve] Timed out for {track.webpage_url}")
        return False
    except Exception as e:
        print(f"[Resolve] Failed for {track.webpage_url}: {e}")
        return False


async def _update_presence(bot_ref: commands.Bot, track: Optional[Track]) -> None:
    try:
        if track:
            await bot_ref.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name=track.title)
            )
        else:
            await bot_ref.change_presence(activity=None)
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

    source       = _make_source(q.current, q.volume, seek_secs, q.bass_boost)
    q.play_start = time.monotonic() - seek_secs
    q.vote_skip_users.clear()

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

    # Cancel any previous live-update task
    if q.np_update_task:
        q.np_update_task.cancel()
        q.np_update_task = None
    q.np_message = None

    if q.announce and q.text_channel and seek_secs == 0:
        view = NowPlayingView(guild.id)
        msg  = await q.text_channel.send(
            embed=_now_playing_embed(q.current, q.loop_mode, q.play_start, q.bass_boost),
            view=view,
        )
        q.np_message = msg
        q.np_update_task = asyncio.create_task(_np_updater(guild.id, q.current))


async def _play_next(guild: discord.Guild) -> None:
    q = queues.get(guild.id)
    if q is None:
        return

    # Seek / restart was requested (volume change, /seek, /replay)
    if q.restart_current:
        q.restart_current = False
        seek = q.seek_to
        q.seek_to = 0
        await _start_playing(guild, q, seek_secs=seek)
        return

    if q.loop_mode == "song" and q.current:
        await _start_playing(guild, q)
        return

    if q.current:
        q.history.append(q.current)
        q.songs_played += 1

    if q.loop_mode == "queue" and q.current:
        q.tracks.append(q.current)

    if not q.tracks:
        q.current = None
        asyncio.create_task(_update_presence(guild._state.client, None))
        if q.np_update_task:
            q.np_update_task.cancel()
            q.np_update_task = None
        if not q.mode_247 and q.voice_client and q.voice_client.is_connected():
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
        if q.mode_247:
            return
        ch = q.voice_client.channel.name if q.voice_client.channel else "unknown"
        await q.voice_client.disconnect()
        queues.pop(guild.id, None)
        print(f"[Auto-leave] Idle timeout in #{ch}")
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
        intents.guilds       = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"📡 Slash commands synced to guild {GUILD_ID}")

    async def on_ready(self):
        print(f"🤖 Logged in as {self.user}")

    async def on_voice_state_update(self, member, before, after):
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
                f"⛔ Music commands only work in <#{MUSIC_CHANNEL_ID}>.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def dj_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(
                f"❌ You need the **{DJ_ROLE_NAME}** role to use this command.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


async def ensure_voice(interaction: discord.Interaction) -> Optional[GuildQueue]:
    # All callers already called defer(), so we must use followup — never response.send_message
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ You need to be in a voice channel first.", ephemeral=True)
        return None

    q       = get_queue(interaction.guild_id)
    channel = interaction.user.voice.channel

    existing_vc = interaction.guild.voice_client
    if not existing_vc or not existing_vc.is_connected():
        if existing_vc:
            try:
                await asyncio.wait_for(existing_vc.disconnect(force=True), timeout=5)
            except Exception:
                pass
        try:
            vc = await asyncio.wait_for(channel.connect(reconnect=True), timeout=20)
            q.voice_client = vc
        except asyncio.TimeoutError:
            print("[Voice] connect() timed out")
            await interaction.followup.send("❌ Took too long to join the voice channel. Try again.", ephemeral=True)
            return None
        except Exception as e:
            print(f"[Voice] Failed to connect: {e}")
            await interaction.followup.send("❌ Couldn't connect to your voice channel. Try again.", ephemeral=True)
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
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{current}", download=False)
            return info.get("entries", [])

    try:
        entries = await asyncio.wait_for(loop.run_in_executor(None, _search), timeout=2.5)
    except Exception:
        return []

    choices = []
    for entry in entries[:10]:
        title  = entry.get("title", "Unknown")
        vid_id = entry.get("id", "")
        if not vid_id: continue
        url = f"https://youtu.be/{vid_id}"
        dur = entry.get("duration")
        if dur:
            m, s = divmod(int(dur), 60)
            label = f"{title} ({m}:{s:02d})"
        else:
            label = title
        choices.append(app_commands.Choice(name=label[:100], value=url[:100]))
    return choices


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="play", description="Play a song or paste a YouTube / Spotify URL or playlist")
@app_commands.describe(query="Song name, YouTube URL, Spotify URL, or playlist link")
@music_channel_only()
async def cmd_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    try:
        q = await ensure_voice(interaction)
        if q is None: return

        uid = interaction.user.id
        if SONG_LIMIT > 0 and user_song_count(q, uid) >= SONG_LIMIT:
            await interaction.followup.send(
                f"❌ You already have **{SONG_LIMIT}** songs queued. Wait for one to finish.", ephemeral=True); return

        is_playlist = ("list=" in query) and (query.startswith("http://") or query.startswith("https://"))

        if is_playlist:
            tracks = await fetch_playlist(query, str(interaction.user), uid)
            if not tracks:
                await interaction.followup.send("❌ Couldn't load that playlist."); return
            start_now = q.current is None and not q.voice_client.is_playing()
            for t in tracks: q.tracks.append(t)
            if start_now and q.tracks:
                q.current = q.tracks.popleft()
                await _start_playing(interaction.guild, q)
            await interaction.followup.send(
                f"{'▶️ Starting' if start_now else '➕ Added'} **{len(tracks)} songs** from the playlist.")
            return

        track = await fetch_track(query, str(interaction.user), uid)
        if track is None:
            await interaction.followup.send("❌ Could not find that song. Try a different search."); return
        if isinstance(track, str):
            await interaction.followup.send(track); return

        if q.voice_client.is_playing() or q.voice_client.is_paused() or q.current is not None:
            q.tracks.append(track)
            embed = discord.Embed(title="➕ Added to Queue",
                                  description=f"**[{track.title}]({track.webpage_url})**",
                                  color=0x5865F2)
            embed.add_field(name="Duration",     value=track.duration,         inline=True)
            embed.add_field(name="Position",     value=str(len(q.tracks)),     inline=True)
            embed.add_field(name="Requested by", value=track.requested_by,     inline=True)
            if track.thumbnail: embed.set_thumbnail(url=track.thumbnail)
            await interaction.followup.send(embed=embed)
        else:
            q.current = track
            await _start_playing(interaction.guild, q)
            await interaction.followup.send("▶️ Starting playback…")

    except Exception as e:
        print(f"[cmd_play] Unhandled error: {e}")
        try:
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
        except Exception:
            pass


@cmd_play.autocomplete("query")
async def play_autocomplete(interaction: discord.Interaction, current: str):
    if not current or len(current) < 2: return []
    return await _search_suggestions(current)


@bot.tree.command(name="playnext", description="Add a song to the front of the queue (plays after current)")
@app_commands.describe(query="Song name or YouTube / Spotify URL")
@music_channel_only()
@dj_only()
async def cmd_playnext(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = await ensure_voice(interaction)
    if q is None: return

    track = await fetch_track(query, str(interaction.user), interaction.user.id)
    if track is None:
        await interaction.followup.send("❌ Could not find that song."); return
    if isinstance(track, str):
        await interaction.followup.send(track); return

    q.tracks.appendleft(track)
    embed = discord.Embed(title="⏫ Playing Next",
                          description=f"**[{track.title}]({track.webpage_url})**",
                          color=0x5865F2)
    embed.add_field(name="Duration", value=track.duration, inline=True)
    if track.thumbnail: embed.set_thumbnail(url=track.thumbnail)
    await interaction.followup.send(embed=embed)


@cmd_playnext.autocomplete("query")
async def playnext_autocomplete(interaction: discord.Interaction, current: str):
    if not current or len(current) < 2: return []
    return await _search_suggestions(current)


@bot.tree.command(name="search", description="Search YouTube and pick from a list of results")
@app_commands.describe(query="What to search for")
@music_channel_only()
async def cmd_search(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = await ensure_voice(interaction)
    if q is None: return

    entries = await search_youtube(query, count=5)
    if not entries:
        await interaction.followup.send("❌ No results found. Try a different search."); return

    lines = []
    for i, e in enumerate(entries[:5], 1):
        title  = e.get("title", "Unknown")
        dur    = e.get("duration")
        vid_id = e.get("id", "")
        url    = f"https://youtu.be/{vid_id}" if vid_id else ""
        lines.append(f"`{i}.` [{title}]({url}) `{format_duration(dur) if dur else '?'}`")

    embed = discord.Embed(title=f"🔍 Results for: {query}",
                          description="\n".join(lines), color=0x5865F2)
    embed.set_footer(text="Select a song from the dropdown below")
    view = SearchView(entries[:5], interaction.guild, q, str(interaction.user), interaction.user.id)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="skip", description="Skip the current song — DJs skip instantly, others vote")
@music_channel_only()
async def cmd_skip(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return

    # DJ → instant skip
    if has_dj_role(interaction.user):
        q.vote_skip_users.clear()
        q.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.")
        return

    # Non-DJ → vote skip
    listeners = [m for m in q.voice_client.channel.members if not m.bot]
    needed    = max(1, math.ceil(len(listeners) / 2))

    if interaction.user.id in q.vote_skip_users:
        await interaction.response.send_message("🗳️ You already voted to skip.", ephemeral=True); return

    q.vote_skip_users.add(interaction.user.id)
    valid_votes = sum(1 for uid in q.vote_skip_users if any(m.id == uid for m in listeners))

    if valid_votes >= needed:
        q.vote_skip_users.clear()
        q.voice_client.stop()
        await interaction.response.send_message(f"⏭️ Vote skip passed! ({valid_votes}/{needed} votes)")
    else:
        await interaction.response.send_message(
            f"🗳️ Vote to skip: **{valid_votes}/{needed}** votes — need {needed - valid_votes} more.")


@bot.tree.command(name="skipto", description="Skip to a specific position in the queue")
@app_commands.describe(position="Queue position to jump to")
@music_channel_only()
@dj_only()
async def cmd_skipto(interaction: discord.Interaction, position: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True); return
    if position < 1 or position > len(q.tracks):
        await interaction.response.send_message(f"❌ Position must be 1–{len(q.tracks)}.", ephemeral=True); return

    lst     = list(q.tracks)
    skipped = lst[:position - 1]
    for t in skipped: q.history.append(t)
    q.tracks = deque(lst[position - 1:])

    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()
        await interaction.response.send_message(f"⏭️ Jumped to position **{position}**.")
    else:
        q.current = q.tracks.popleft()
        await _start_playing(interaction.guild, q)
        await interaction.response.send_message(f"▶️ Playing position **{position}**.")


@bot.tree.command(name="seek", description="Jump to a specific time in the current song")
@app_commands.describe(timestamp="Time to seek to, e.g. 1:30 or 90")
@music_channel_only()
@dj_only()
async def cmd_seek(interaction: discord.Interaction, timestamp: str):
    q = queues.get(interaction.guild_id)
    if not q or not q.current:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return

    secs = parse_time(timestamp)
    if secs is None:
        await interaction.response.send_message("❌ Invalid time. Use format `1:30` or `90`.", ephemeral=True); return

    dur = q.current.duration_secs or 0
    if dur and secs >= dur:
        await interaction.response.send_message(f"❌ Song is only {q.current.duration} long.", ephemeral=True); return

    q.restart_current = True
    q.seek_to         = secs

    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()

    await interaction.response.send_message(f"⏩ Seeking to **{format_duration(secs)}**…")


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
@music_channel_only()
@dj_only()
async def cmd_stop(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True); return
    if q.idle_task:       q.idle_task.cancel()
    if q.np_update_task:  q.np_update_task.cancel()
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
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return
    q.voice_client.pause()
    await interaction.response.send_message("⏸️ Paused.")


@bot.tree.command(name="resume", description="Resume the paused song")
@music_channel_only()
@dj_only()
async def cmd_resume(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_paused():
        await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True); return
    q.voice_client.resume()
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="replay", description="Restart the current song from the beginning")
@music_channel_only()
@dj_only()
async def cmd_replay(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.current:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return

    q.restart_current = True
    q.seek_to         = 0

    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()

    await interaction.response.send_message("🔄 Restarting from the beginning.")


@bot.tree.command(name="loop", description="Toggle loop mode: off → song → queue")
@music_channel_only()
@dj_only()
async def cmd_loop(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return
    modes = ["off", "song", "queue"]
    q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % len(modes)]
    labels = {"off": "🔁 Loop is now **Off**",
              "song": "🔂 Now looping the **current song**",
              "queue": "🔁 Now looping the **entire queue**"}
    await interaction.response.send_message(labels[q.loop_mode])


@bot.tree.command(name="shuffle", description="Shuffle the upcoming queue")
@music_channel_only()
@dj_only()
async def cmd_shuffle(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True); return
    lst = list(q.tracks); random.shuffle(lst); q.tracks = deque(lst)
    await interaction.response.send_message(f"🔀 Shuffled **{len(lst)}** songs.")


@bot.tree.command(name="clear", description="Clear all upcoming songs without stopping the current one")
@music_channel_only()
@dj_only()
async def cmd_clear(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is already empty.", ephemeral=True); return
    count = len(q.tracks); q.tracks.clear()
    await interaction.response.send_message(f"🗑️ Cleared **{count}** song(s) from the queue.")


@bot.tree.command(name="remove", description="Remove a song from the queue by its position")
@app_commands.describe(position="Position number from /queue")
@music_channel_only()
@dj_only()
async def cmd_remove(interaction: discord.Interaction, position: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True); return
    if position < 1 or position > len(q.tracks):
        await interaction.response.send_message(f"❌ Position must be 1–{len(q.tracks)}.", ephemeral=True); return
    lst = list(q.tracks); removed = lst.pop(position - 1); q.tracks = deque(lst)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}**.")


@bot.tree.command(name="move", description="Move a song to a different position in the queue")
@app_commands.describe(from_pos="Current position", to_pos="New position")
@music_channel_only()
@dj_only()
async def cmd_move(interaction: discord.Interaction, from_pos: int, to_pos: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        await interaction.response.send_message("❌ The queue is empty.", ephemeral=True); return
    n = len(q.tracks)
    if not (1 <= from_pos <= n) or not (1 <= to_pos <= n):
        await interaction.response.send_message(f"❌ Positions must be 1–{n}.", ephemeral=True); return
    lst = list(q.tracks); t = lst.pop(from_pos - 1); lst.insert(to_pos - 1, t); q.tracks = deque(lst)
    await interaction.response.send_message(f"↕️ Moved **{t.title}** to position **{to_pos}**.")


@bot.tree.command(name="volume", description="Set the volume (1–100) — applies immediately")
@app_commands.describe(level="Volume level between 1 and 100")
@music_channel_only()
@dj_only()
async def cmd_volume(interaction: discord.Interaction, level: int):
    if not 1 <= level <= 100:
        await interaction.response.send_message("❌ Volume must be 1–100.", ephemeral=True); return
    q = queues.get(interaction.guild_id)
    if not q:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True); return

    old     = q.volume
    q.volume = level / 100

    if (q.voice_client and q.current
            and (q.voice_client.is_playing() or q.voice_client.is_paused())
            and abs(old - q.volume) > 0.005):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()
        await interaction.response.send_message(f"🔊 Volume set to **{level}%** — applied immediately.")
    else:
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")


@bot.tree.command(name="bass", description="Toggle bass boost on/off")
@music_channel_only()
@dj_only()
async def cmd_bass(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True); return

    q.bass_boost = not q.bass_boost

    if q.voice_client and q.current and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()

    state = "🔊 **Bass boost ON**" if q.bass_boost else "🔈 **Bass boost OFF**"
    await interaction.response.send_message(state)


@bot.tree.command(name="247", description="Toggle 24/7 mode — bot stays in channel even when idle")
@music_channel_only()
@dj_only()
async def cmd_247(interaction: discord.Interaction):
    q = get_queue(interaction.guild_id)
    q.mode_247 = not q.mode_247
    state = "✅ **24/7 mode ON** — I'll stay in the channel even when nothing is playing." if q.mode_247 \
        else "❌ **24/7 mode OFF** — I'll leave after 5 minutes of inactivity."
    await interaction.response.send_message(state)


@bot.tree.command(name="announce", description="Toggle Now Playing announcements for each song")
@music_channel_only()
@dj_only()
async def cmd_announce(interaction: discord.Interaction):
    q = get_queue(interaction.guild_id)
    q.announce = not q.announce
    state = "✅ Now Playing cards **enabled**." if q.announce else "🔕 Now Playing cards **disabled**."
    await interaction.response.send_message(state)


@bot.tree.command(name="queue", description="Show the current queue")
@music_channel_only()
async def cmd_queue(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or (q.current is None and not q.tracks):
        await interaction.response.send_message("📭 The queue is empty.", ephemeral=True); return
    view = QueueView(q)
    await interaction.response.send_message(embed=view._build_embed(), view=view)


@bot.tree.command(name="nowplaying", description="Show what's currently playing")
@music_channel_only()
async def cmd_nowplaying(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or q.current is None:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True); return
    view = NowPlayingView(interaction.guild_id)
    await interaction.response.send_message(
        embed=_now_playing_embed(q.current, q.loop_mode, q.play_start, q.bass_boost), view=view)


@bot.tree.command(name="history", description="Show the last 10 songs that were played")
@music_channel_only()
async def cmd_history(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.history:
        await interaction.response.send_message("📭 No history yet.", ephemeral=True); return
    lines = [
        f"`{i}.` [{t.title}]({t.webpage_url}) `{t.duration}` — {t.requested_by}"
        for i, t in enumerate(reversed(list(q.history)), 1)
    ]
    embed = discord.Embed(title="📜 Recently Played", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show bot statistics for this session")
@music_channel_only()
async def cmd_stats(interaction: discord.Interaction):
    uptime_secs = int(time.monotonic() - BOT_START)
    h = uptime_secs // 3600; m = (uptime_secs % 3600) // 60; s = uptime_secs % 60
    uptime_str  = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    q = queues.get(interaction.guild_id)
    songs_played = q.songs_played if q else 0
    in_queue     = len(q.tracks) if q else 0
    current      = q.current.title if q and q.current else "Nothing"
    mode_247     = "Yes" if (q and q.mode_247) else "No"
    bass         = "On" if (q and q.bass_boost) else "Off"
    vol          = f"{int((q.volume if q else 1.0) * 100)}%"

    embed = discord.Embed(title="📊 Bot Stats", color=0x5865F2)
    embed.add_field(name="Uptime",        value=uptime_str,       inline=True)
    embed.add_field(name="Songs Played",  value=str(songs_played), inline=True)
    embed.add_field(name="In Queue",      value=str(in_queue),    inline=True)
    embed.add_field(name="Now Playing",   value=current,          inline=False)
    embed.add_field(name="Volume",        value=vol,              inline=True)
    embed.add_field(name="Bass Boost",    value=bass,             inline=True)
    embed.add_field(name="24/7 Mode",     value=mode_247,         inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show all commands and tips")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="🎵 Different Music — Commands", color=0x5865F2)
    embed.add_field(name="▶️ Playback",
        value=("`/play` — Search or paste a YouTube/Spotify URL or playlist\n"
               "`/playnext` — Queue a song to play right after the current one\n"
               "`/search` — Pick from a 5-result dropdown\n"
               "`/pause` `/resume` `/stop`\n"
               "`/replay` — Restart current song\n"
               "`/seek <time>` — Jump to e.g. `1:30`"), inline=False)
    embed.add_field(name="📋 Queue",
        value=("`/queue` — Paginated queue with total duration\n"
               "`/nowplaying` — Live progress bar for current song\n"
               "`/history` — Last 10 songs played\n"
               "`/skipto <pos>` `/remove <pos>` `/move <from> <to>` `/clear`"), inline=False)
    embed.add_field(name="🎛️ Controls",
        value=("`/skip` — DJ skips instantly; others cast a vote\n"
               "`/loop` — Off → Song → Queue\n"
               "`/shuffle` `/volume <1-100>` `/bass` — toggle bass boost"), inline=False)
    embed.add_field(name="⚙️ Settings",
        value=(f"`/247` — Stay in channel forever (currently by guild)\n"
               "`/announce` — Toggle Now Playing cards\n"
               "`/stats` — Uptime, songs played, current settings"), inline=False)
    embed.add_field(name="💡 Tips",
        value=(f"• DJ commands require the **{DJ_ROLE_NAME}** role\n"
               f"• Max {SONG_LIMIT} songs per person in queue\n"
               "• Now Playing card auto-updates every 20 s\n"
               "• Paste a Spotify link and the bot finds it on YouTube\n"
               "• Buttons on the Now Playing card: ⏸️ ⏭️ 🔁 🔀"), inline=False)
    embed.set_footer(text="Different Music Bot")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

bot.run(TOKEN)
