# Different Music Bot — main.py
# Production-ready Discord music bot: discord.py + yt-dlp

import asyncio
import json
import math
import os
import random
import re
import time
import traceback
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOKEN            = os.getenv("DISCORD_TOKEN")
MUSIC_CHANNEL_ID = int(os.getenv("MUSIC_CHANNEL_ID", "1487195424111726743"))
GUILD_ID         = 850386896509337710
DJ_ROLE_NAME     = os.getenv("DJ_ROLE_NAME", "DJ")
SONG_LIMIT       = int(os.getenv("SONG_LIMIT_PER_USER", "5"))  # 0 = unlimited
IDLE_TIMEOUT     = 300    # seconds of silence before auto-leaving
STREAM_TTL       = 18000  # re-fetch stream URL after 5 hours (YouTube URLs expire)
BOT_START        = time.monotonic()
# Any valid Twitch URL — only needed so Discord shows the purple streaming indicator
TWITCH_URL       = os.getenv("TWITCH_URL", "https://twitch.tv/discord")
# Where to store saved playlists on disk
PLAYLIST_DIR     = Path(os.getenv("PLAYLIST_DIR", os.path.expanduser("~/discord-bot/playlists")))

if not TOKEN:
    raise ValueError("DISCORD_TOKEN is not set in environment")

# ---------------------------------------------------------------------------
# FFmpeg options — reconnect flags prevent drop-outs mid-stream
# ---------------------------------------------------------------------------

FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-thread_queue_size 512"
)

# ---------------------------------------------------------------------------
# Audio filters & EQ presets
# ---------------------------------------------------------------------------

# Each value is a raw FFmpeg -af filter string
AUDIO_FILTERS: dict[str, str] = {
    "nightcore": "asetrate=44100*1.25,atempo=0.8",          # faster + higher pitch
    "vaporwave": "asetrate=44100*0.75,atempo=1.333",         # slower + lower pitch
    "slowed":    "asetrate=44100*0.9,atempo=1.111",          # slightly slower/lower
    "8d":        "apulsator=hz=0.08",                        # panning 8D effect
    "karaoke":   "pan=stereo|c0=c0-c1|c1=c1-c0",            # centre channel removal
    "reverb":    "aecho=0.8:0.9:500|1000:0.5|0.3",          # echo/reverb
}

EQ_PRESETS: dict[str, Optional[str]] = {
    "flat":      None,
    "bass":      "bass=g=6",
    "treble":    "treble=g=5",
    "pop":       "equalizer=f=100:width_type=h:width=200:g=3,equalizer=f=4000:width_type=h:width=2000:g=4",
    "rock":      "bass=g=4,equalizer=f=3000:width_type=h:width=1000:g=3",
    "jazz":      "equalizer=f=300:width_type=h:width=300:g=2,equalizer=f=6000:width_type=h:width=3000:g=3",
    "classical": "treble=g=4,equalizer=f=250:width_type=h:width=300:g=2",
}


def _atempo_chain(speed: float) -> str:
    """Build a chain of atempo filters — handles values outside the 0.5–2.0 range."""
    filters: list[str] = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining *= 2.0
    filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Track:
    title:           str
    webpage_url:     str
    stream_url:      str
    duration:        str
    duration_secs:   Optional[int]
    thumbnail:       str
    requested_by:    str
    requested_by_id: int   = 0
    # Timestamp when the stream URL was fetched — used for TTL expiry checks
    fetched_at:      float = field(default_factory=time.monotonic)


@dataclass
class GuildQueue:
    tracks:          deque                         = field(default_factory=deque)
    current:         Optional[Track]               = None
    voice_client:    Optional[discord.VoiceClient] = None
    text_channel:    Optional[discord.TextChannel] = None
    volume:          float                         = 1.0
    loop_mode:       str                           = "off"   # "off" | "song" | "queue"
    play_start:      Optional[float]               = None
    idle_task:       Optional[asyncio.Task]        = None
    history:         deque                         = field(default_factory=lambda: deque(maxlen=10))
    songs_played:    int                           = 0
    # Feature flags
    mode_247:        bool                          = False
    announce:        bool                          = True
    bass_boost:      bool                          = False
    autoplay:        bool                          = False
    # Audio effects (applied via FFmpeg filters)
    audio_filter:    Optional[str]                 = None   # key in AUDIO_FILTERS, or None
    eq_preset:       str                           = "flat" # key in EQ_PRESETS
    speed:           float                         = 1.0    # playback speed multiplier
    # Seek/restart flags (set before stopping to restart at a given position)
    restart_current: bool                          = False
    seek_to:         int                           = 0
    # Pause-time tracking so progress bar stays accurate while paused
    paused_at:       Optional[float]               = None
    # Vote-skip state
    vote_skip_users: set                           = field(default_factory=set)
    # Live Now Playing embed tracking
    np_message:      Optional[discord.Message]     = None
    np_update_task:  Optional[asyncio.Task]        = None


# Global state
queues:      dict[int, GuildQueue]   = {}
_play_locks: dict[int, asyncio.Lock] = {}


def get_queue(guild_id: int) -> GuildQueue:
    if guild_id not in queues:
        queues[guild_id] = GuildQueue()
    return queues[guild_id]


def get_play_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _play_locks:
        _play_locks[guild_id] = asyncio.Lock()
    return _play_locks[guild_id]


def _cleanup_guild(guild_id: int) -> None:
    """Remove queue and lock state for a guild (called on disconnect/stop)."""
    queues.pop(guild_id, None)
    _play_locks.pop(guild_id, None)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_dur(seconds) -> str:
    """Convert seconds to m:ss or h:mm:ss string."""
    if seconds is None:
        return "?:??"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def progress_bar(elapsed: int, total: int, width: int = 17) -> str:
    """Spotify-style progress bar: 0:31 ━━━━●──────── 3:17"""
    if not total:
        return ""
    ratio  = min(elapsed / total, 1.0)
    dot_pos = round(ratio * width)
    bar = "━" * dot_pos + "●" + "─" * (width - dot_pos)
    return f"`{fmt_dur(elapsed)}` {bar} `{fmt_dur(total)}`"


def parse_time(s: str) -> Optional[int]:
    """Parse '1:30', '1:02:30', or '90' into total seconds."""
    try:
        parts = s.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(s)
    except (ValueError, IndexError):
        return None


def est_wait(q: GuildQueue, up_to_index: Optional[int] = None) -> int:
    """Estimate seconds until a queued track plays.
    If up_to_index is given, compute wait for that position (0-based); otherwise for the last."""
    lst = list(q.tracks)
    end = len(lst) if up_to_index is None else up_to_index
    secs = sum(t.duration_secs or 0 for t in lst[:end])
    if q.current and q.play_start and q.current.duration_secs:
        elapsed = int(time.monotonic() - q.play_start)
        secs += max(0, q.current.duration_secs - elapsed)
    return secs


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

def has_dj_role(member: discord.Member) -> bool:
    """Server owner and admins always pass; then check for configured DJ role."""
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.administrator:
        return True
    role = discord.utils.get(member.guild.roles, name=DJ_ROLE_NAME)
    if role is None:
        return True   # DJ role not configured → allow everyone
    return role in member.roles


def user_song_count(q: GuildQueue, user_id: int) -> int:
    return sum(1 for t in q.tracks if t.requested_by_id == user_id)


def _same_vc(interaction: discord.Interaction, q: GuildQueue) -> bool:
    """True if the user is in the same voice channel as the bot (or bot not connected)."""
    if not q.voice_client or not q.voice_client.channel:
        return True
    if not interaction.user.voice or not interaction.user.voice.channel:
        return False
    return interaction.user.voice.channel == q.voice_client.channel


# ---------------------------------------------------------------------------
# Spotify → YouTube title lookup (no API key required)
# ---------------------------------------------------------------------------

def _is_spotify(url: str) -> bool:
    return "open.spotify.com" in url


async def _spotify_title(url: str) -> Optional[str]:
    """Fetch track title from Spotify's public oEmbed endpoint."""
    loop = asyncio.get_event_loop()

    def _fetch():
        req = urllib.request.Request(
            f"https://open.spotify.com/oembed?url={url}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read()).get("title")

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=8)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

YDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[protocol=https]/bestaudio",
    "quiet":       True,
    "no_warnings": True,
    "extract_flat": False,
    "noplaylist":  True,
}

UNSUPPORTED_DOMAINS = ("music.apple.com", "tidal.com", "deezer.com")


def _info_to_track(info: dict, requested_by: str, requested_by_id: int = 0) -> Track:
    thumbs    = info.get("thumbnails") or []
    thumbnail = thumbs[-1]["url"] if thumbs else info.get("thumbnail", "")
    stream_url = info.get("url", "")
    if not stream_url:
        for fmt in reversed(info.get("formats", [])):
            if fmt.get("acodec") != "none" and fmt.get("url"):
                stream_url = fmt["url"]
                break
    dur = info.get("duration")
    return Track(
        title           = info.get("title", "Unknown"),
        webpage_url     = info.get("webpage_url", ""),
        stream_url      = stream_url,
        duration        = fmt_dur(dur),
        duration_secs   = int(dur) if dur else None,
        thumbnail       = thumbnail,
        requested_by    = requested_by,
        requested_by_id = requested_by_id,
        fetched_at      = time.monotonic(),
    )


async def fetch_track(query: str, requested_by: str,
                      requested_by_id: int = 0) -> "Optional[Union[Track, str]]":
    """Fetch a single track. Returns Track, an error string, or None on failure."""
    is_url = query.startswith(("http://", "https://"))

    if is_url and any(d in query for d in UNSUPPORTED_DOMAINS):
        return "❌ That platform isn't supported. Use a YouTube URL or search by name."

    # Convert Spotify link → song title for YouTube search
    if is_url and _is_spotify(query):
        title = await _spotify_title(query)
        if not title:
            return "❌ Couldn't read that Spotify link. Try searching by song name instead."
        query, is_url = title, False

    target = query if is_url else f"ytsearch1:{query}"
    loop   = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(target, download=False)
            return info["entries"][0] if "entries" in info else info

    try:
        info = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=20)
        return _info_to_track(info, requested_by, requested_by_id)
    except asyncio.TimeoutError:
        print(f"[yt-dlp] Timed out: {target}")
        return None
    except Exception as e:
        print(f"[yt-dlp] Error fetching '{target}': {e}")
        return None


async def fetch_playlist(url: str, requested_by: str,
                          requested_by_id: int = 0) -> list[Track]:
    """Load up to 25 tracks from a playlist (stream URLs resolved lazily at play time)."""
    loop = asyncio.get_event_loop()

    def _extract():
        opts = {**YDL_OPTS, "extract_flat": True, "noplaylist": False, "playlistend": 25}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("entries", [])

    try:
        entries = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=30)
    except Exception as e:
        print(f"[yt-dlp Playlist] Error: {e}")
        return []

    tracks = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        dur = entry.get("duration")
        tracks.append(Track(
            title           = entry.get("title", "Unknown"),
            webpage_url     = f"https://youtu.be/{entry['id']}",
            stream_url      = "",   # resolved lazily in _resolve_stream when played
            duration        = fmt_dur(dur),
            duration_secs   = int(dur) if dur else None,
            thumbnail       = entry.get("thumbnail", ""),
            requested_by    = requested_by,
            requested_by_id = requested_by_id,
        ))
    return tracks


async def search_youtube(query: str, count: int = 5) -> list[dict]:
    """Return raw yt-dlp entries for a YouTube search."""
    loop = asyncio.get_event_loop()

    def _search():
        opts = {**YDL_OPTS, "extract_flat": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            return info.get("entries", [])

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _search), timeout=8)
    except Exception:
        return []


async def fetch_autoplay_track(current_title: str, history_urls: set[str]) -> Optional[Track]:
    """Fetch a related YouTube track for autoplay, avoiding recently played songs."""
    loop = asyncio.get_event_loop()

    def _search():
        opts = {**YDL_OPTS, "extract_flat": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch8:{current_title}", download=False)
            return info.get("entries", [])

    try:
        entries = await asyncio.wait_for(loop.run_in_executor(None, _search), timeout=10)
    except Exception:
        return None

    # Pick the first result that wasn't recently played (skip index 0 = same song)
    for entry in entries[1:]:
        vid_id = entry.get("id", "")
        if not vid_id:
            continue
        url = f"https://youtu.be/{vid_id}"
        if url not in history_urls:
            dur = entry.get("duration")
            return Track(
                title           = entry.get("title", "Unknown"),
                webpage_url     = url,
                stream_url      = "",
                duration        = fmt_dur(dur),
                duration_secs   = int(dur) if dur else None,
                thumbnail       = entry.get("thumbnail", ""),
                requested_by    = "Autoplay",
                requested_by_id = 0,
            )
    return None


# ---------------------------------------------------------------------------
# Playback helpers
# ---------------------------------------------------------------------------

def _make_source(track: Track, volume: float, seek_secs: int = 0,
                 bass: bool = False, audio_filter: Optional[str] = None,
                 eq_preset: str = "flat", speed: float = 1.0) -> discord.FFmpegOpusAudio:
    before_opts = FFMPEG_BEFORE_OPTS
    if seek_secs > 0:
        before_opts = f"-ss {seek_secs} " + before_opts

    filters: list[str] = []

    # 1. Audio filter (nightcore, vaporwave, etc.) — takes priority over bass_boost
    if audio_filter and audio_filter in AUDIO_FILTERS:
        filters.append(AUDIO_FILTERS[audio_filter])
    else:
        # 2. Speed control (only when no audio_filter, since those handle tempo internally)
        if abs(speed - 1.0) > 0.01:
            filters.append(_atempo_chain(speed))
        # 3. Legacy bass boost toggle
        if bass:
            filters.append("bass=g=6")

    # 4. EQ preset (stacked on top of everything)
    eq = EQ_PRESETS.get(eq_preset)
    if eq:
        filters.append(eq)

    # 5. Volume
    if abs(volume - 1.0) > 0.005:
        filters.append(f"volume={volume}")

    options = "-vn"
    if filters:
        options += f" -af {','.join(filters)}"

    return discord.FFmpegOpusAudio(
        track.stream_url,
        before_options=before_opts,
        options=options,
    )


async def _resolve_stream(track: Track) -> bool:
    """Ensure the track has a valid, non-expired stream URL.
    Re-fetches if the URL is missing or older than STREAM_TTL seconds."""
    expired = time.monotonic() - track.fetched_at > STREAM_TTL
    if track.stream_url and not expired:
        return True

    if expired:
        print(f"[Resolve] Stream URL expired for '{track.title}', re-fetching…")
        track.stream_url = ""

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
        track.fetched_at    = time.monotonic()
        return bool(track.stream_url)
    except asyncio.TimeoutError:
        print(f"[Resolve] Timed out for '{track.title}'")
        return False
    except Exception as e:
        print(f"[Resolve] Failed for '{track.title}': {e}")
        return False


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

LOOP_LABELS = {"off": "Off", "song": "🔂 Song", "queue": "🔁 Queue"}


def _effect_summary(q: GuildQueue) -> str:
    """One-line summary of active audio effects for the NP embed."""
    parts = []
    if q.audio_filter:
        labels = {
            "nightcore": "🌙 Nightcore", "vaporwave": "🌊 Vaporwave",
            "slowed": "🐢 Slowed",       "8d": "🎧 8D",
            "karaoke": "🎤 Karaoke",    "reverb": "🔊 Reverb",
        }
        parts.append(labels.get(q.audio_filter, q.audio_filter.title()))
    if abs(q.speed - 1.0) > 0.01 and not q.audio_filter:
        parts.append(f"⏩ {q.speed:.1f}x")
    if q.eq_preset != "flat":
        parts.append(f"🎚 {q.eq_preset.title()}")
    if q.bass_boost and not q.audio_filter:
        parts.append("🔊 Bass")
    if abs(q.volume - 1.0) > 0.005:
        parts.append(f"🔈 {int(q.volume * 100)}%")
    return "  ".join(parts) if parts else "None"


def _np_embed(track: Track, q: GuildQueue,
              play_start: Optional[float] = None) -> discord.Embed:
    embed = discord.Embed(
        title       = "🎵 Now Playing",
        description = f"**[{track.title}]({track.webpage_url})**",
        color       = 0x5865F2,
    )
    if play_start and track.duration_secs:
        elapsed = max(0, min(int(time.monotonic() - play_start), track.duration_secs))
        embed.add_field(name="\u200b", value=progress_bar(elapsed, track.duration_secs), inline=False)
    else:
        embed.add_field(name="Duration", value=track.duration, inline=True)

    embed.add_field(name="Requested by", value=track.requested_by, inline=True)
    loop_val = LOOP_LABELS.get(q.loop_mode, "Off")
    embed.add_field(name="Loop", value=loop_val, inline=True)

    effects = _effect_summary(q)
    embed.add_field(name="Effects", value=effects, inline=False)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def _queued_embed(track: Track, q: GuildQueue) -> discord.Embed:
    """Embed shown when a track is added to an already-playing queue."""
    pos  = len(q.tracks)
    wait = est_wait(q)
    embed = discord.Embed(
        title       = "➕ Added to Queue",
        description = f"**[{track.title}]({track.webpage_url})**",
        color       = 0x5865F2,
    )
    embed.add_field(name="Duration",     value=track.duration,    inline=True)
    embed.add_field(name="Position",     value=f"#{pos}",         inline=True)
    embed.add_field(name="Est. wait",    value=fmt_dur(wait),     inline=True)
    embed.add_field(name="Requested by", value=track.requested_by, inline=False)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


# ---------------------------------------------------------------------------
# Lyrics helper — uses lyrics.ovh (free, no API key)
# ---------------------------------------------------------------------------

async def fetch_lyrics(title: str) -> Optional[str]:
    """Fetch lyrics for a song title. Tries to parse 'Artist - Song' format first."""
    loop = asyncio.get_event_loop()

    # Try to split "Artist - Song" or "Song - Artist" patterns
    if " - " in title:
        parts = title.split(" - ", 1)
        candidates = [
            (parts[0].strip(), parts[1].strip()),  # Artist - Song
            (parts[1].strip(), parts[0].strip()),  # Song - Artist (reversed)
        ]
    else:
        # No separator — use the title as song, leave artist blank (lyrics.ovh handles it)
        candidates = [("", title)]

    def _fetch(artist: str, song: str) -> Optional[str]:
        # Clean up common YouTube title noise before querying
        song   = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", song).strip()
        artist = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", artist).strip()
        if artist:
            url = (f"https://api.lyrics.ovh/v1/"
                   f"{urllib.parse.quote(artist)}/{urllib.parse.quote(song)}")
        else:
            url = f"https://api.lyrics.ovh/v1/Unknown/{urllib.parse.quote(song)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
                return data.get("lyrics")
        except Exception:
            return None

    for artist, song in candidates:
        try:
            lyrics = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch, artist, song), timeout=8
            )
            if lyrics and len(lyrics) > 20:
                return lyrics.strip()
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Saved playlists — stored as JSON files per guild
# ---------------------------------------------------------------------------

def _playlist_path(guild_id: int, name: str) -> Path:
    return PLAYLIST_DIR / str(guild_id) / f"{name}.json"


def _save_playlist(guild_id: int, name: str, tracks: list[Track]) -> None:
    path = _playlist_path(guild_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {"title": t.title, "webpage_url": t.webpage_url,
         "duration": t.duration, "duration_secs": t.duration_secs,
         "thumbnail": t.thumbnail, "requested_by": t.requested_by}
        for t in tracks
    ]
    path.write_text(json.dumps(data, indent=2))


def _load_playlist(guild_id: int, name: str) -> Optional[list[Track]]:
    path = _playlist_path(guild_id, name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return [
            Track(
                title           = e["title"],
                webpage_url     = e["webpage_url"],
                stream_url      = "",   # resolved lazily at play time
                duration        = e.get("duration", "?:??"),
                duration_secs   = e.get("duration_secs"),
                thumbnail       = e.get("thumbnail", ""),
                requested_by    = e.get("requested_by", "Playlist"),
                requested_by_id = 0,
            )
            for e in data
        ]
    except Exception:
        return None


def _list_playlists(guild_id: int) -> list[str]:
    folder = PLAYLIST_DIR / str(guild_id)
    if not folder.exists():
        return []
    return [p.stem for p in sorted(folder.glob("*.json"))]


def _delete_playlist(guild_id: int, name: str) -> bool:
    path = _playlist_path(guild_id, name)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Live Now Playing updater
# ---------------------------------------------------------------------------

async def _np_updater(guild_id: int, track: Track) -> None:
    """Edit the Now Playing embed every 20 s with a refreshed progress bar."""
    await asyncio.sleep(20)
    while True:
        q = queues.get(guild_id)
        if not q or q.current is not track or not q.np_message:
            return
        try:
            await q.np_message.edit(
                embed=_np_embed(track, q, q.play_start)
            )
        except discord.NotFound:
            q.np_message = None
            return
        except (discord.Forbidden, Exception):
            return
        await asyncio.sleep(20)


def _cancel_np_tasks(q: GuildQueue) -> None:
    """Cancel the live NP updater task and clear the tracked message."""
    if q.np_update_task and not q.np_update_task.done():
        q.np_update_task.cancel()
    q.np_update_task = None
    q.np_message     = None


def _register_np(q: GuildQueue, msg: discord.Message, track: Track, guild_id: int) -> None:
    """Start tracking a message as the live Now Playing card."""
    _cancel_np_tasks(q)
    q.np_message     = msg
    q.np_update_task = asyncio.create_task(_np_updater(guild_id, track))


async def _refresh_np_embed(q: GuildQueue) -> None:
    """Immediately edit the live NP card to reflect a settings change (filter/eq/speed/etc)."""
    if not q.np_message or not q.current:
        return
    try:
        await q.np_message.edit(embed=_np_embed(q.current, q, q.play_start))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Interactive buttons — Now Playing card
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
            return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
        if not _same_vc(interaction, q):
            return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)
        if q.voice_client.is_playing():
            q.voice_client.pause()
            q.paused_at  = time.monotonic()
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif q.voice_client.is_paused():
            if q.paused_at and q.play_start:
                q.play_start += time.monotonic() - q.paused_at
            q.paused_at  = None
            q.voice_client.resume()
            button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
            return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        if not _same_vc(interaction, q):
            return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)
        if not has_dj_role(interaction.user):
            return await interaction.response.send_message(
                f"❌ You need the **{DJ_ROLE_NAME}** role to skip.", ephemeral=True)
        q.vote_skip_users.clear()
        q.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q:
            return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        if not has_dj_role(interaction.user):
            return await interaction.response.send_message(
                f"❌ You need the **{DJ_ROLE_NAME}** role.", ephemeral=True)
        modes = ["off", "song", "queue"]
        q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % len(modes)]
        labels = {"off": "🔁 Loop: Off", "song": "🔂 Loop: Song", "queue": "🔁 Loop: Queue"}
        await interaction.response.send_message(f"✅ {labels[q.loop_mode]}", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="np_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self._q()
        if not q or not q.tracks:
            return await interaction.response.send_message("❌ Queue is empty.", ephemeral=True)
        if not has_dj_role(interaction.user):
            return await interaction.response.send_message(
                f"❌ You need the **{DJ_ROLE_NAME}** role.", ephemeral=True)
        lst = list(q.tracks)
        random.shuffle(lst)
        q.tracks = deque(lst)
        await interaction.response.send_message("🔀 Queue shuffled!", ephemeral=True)


# ---------------------------------------------------------------------------
# Paginated queue view — shows per-song estimated wait time
# ---------------------------------------------------------------------------

TRACKS_PER_PAGE = 10


class QueueView(discord.ui.View):
    def __init__(self, q: GuildQueue):
        super().__init__(timeout=60)
        self.q    = q
        self.page = 0
        self._update_buttons()

    def _total_pages(self) -> int:
        return max(1, math.ceil(len(self.q.tracks) / TRACKS_PER_PAGE))

    def _update_buttons(self) -> None:
        total = self._total_pages()
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= total - 1

    def _build_embed(self) -> discord.Embed:
        lines = []
        if self.q.current and self.page == 0:
            lines.append(
                f"**▶️ Now Playing:** [{self.q.current.title}]({self.q.current.webpage_url}) "
                f"`{self.q.current.duration}` — {self.q.current.requested_by}"
            )

        lst   = list(self.q.tracks)
        start = self.page * TRACKS_PER_PAGE
        # Pre-compute cumulative wait times for the visible slice
        for i, t in enumerate(lst[start:start + TRACKS_PER_PAGE], start):
            wait = est_wait(self.q, up_to_index=i)
            lines.append(
                f"`{i+1}.` [{t.title}]({t.webpage_url}) `{t.duration}` "
                f"— {t.requested_by} *(in {fmt_dur(wait)})*"
            )

        total_secs = sum(t.duration_secs or 0 for t in self.q.tracks)
        autoplay_str = "  •  🔄 Autoplay ON" if self.q.autoplay else ""
        embed = discord.Embed(
            title       = "🎵 Queue",
            description = "\n".join(lines) if lines else "The queue is empty.",
            color       = 0x5865F2,
        )
        embed.set_footer(
            text=f"Page {self.page+1}/{self._total_pages()} • "
                 f"{len(self.q.tracks)} song(s) • Total: {fmt_dur(total_secs) if total_secs else '?'}"
                 f"{autoplay_str}"
        )
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self._total_pages() - 1:
            self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


# ---------------------------------------------------------------------------
# Search dropdown view
# ---------------------------------------------------------------------------

class SearchView(discord.ui.View):
    def __init__(self, entries: list, guild: discord.Guild, q: GuildQueue,
                 requested_by: str, requested_by_id: int):
        super().__init__(timeout=60)
        self.entries         = entries
        self.guild           = guild
        self.q               = q
        self.requested_by    = requested_by
        self.requested_by_id = requested_by_id

        options = [
            discord.SelectOption(
                label       = f"{i+1}. {e.get('title','Unknown')[:75]}",
                description = fmt_dur(e.get("duration")) if e.get("duration") else "Unknown duration",
                value       = str(i),
            )
            for i, e in enumerate(entries[:5])
        ]
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
            return await interaction.followup.send("❌ Couldn't load that track.", ephemeral=True)

        if self.q.voice_client and (self.q.voice_client.is_playing() or
                                     self.q.voice_client.is_paused() or self.q.current):
            self.q.tracks.append(track)
            await interaction.followup.send(embed=_queued_embed(track, self.q))
        else:
            self.q.current = track
            await _start_playing(self.guild, self.q, send_np=False)
            view = NowPlayingView(self.guild.id)
            msg  = await interaction.followup.send(
                embed=_np_embed(self.q.current, self.q, self.q.play_start),
                view=view,
            )
            _register_np(self.q, msg, track, self.guild.id)
        self.stop()


# ---------------------------------------------------------------------------
# Lyrics paginator
# ---------------------------------------------------------------------------

class LyricsView(discord.ui.View):
    def __init__(self, pages: list[str], title: str, thumbnail: str = ""):
        super().__init__(timeout=120)
        self.pages     = pages
        self.title     = title
        self.thumbnail = thumbnail
        self.page      = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= len(self.pages) - 1

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title       = f"📜 {self.title}",
            description = self.pages[self.page],
            color       = 0x5865F2,
        )
        embed.set_footer(text=f"Page {self.page+1}/{len(self.pages)} • Powered by lyrics.ovh")
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < len(self.pages) - 1:
            self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


# ---------------------------------------------------------------------------
# Core playback engine
# ---------------------------------------------------------------------------

# Regex to strip common YouTube title noise for the activity status
_TITLE_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visualizer)?|"
    r"lyrics?|audio|hd|hq|4k|visualizer|live\s*(?:version|performance)?|"
    r"explicit|clean\s*version?|extended\s*(?:version|mix)?|"
    r"radio\s*edit|full\s*version?|remastered|mv|\d{4}"
    r")\s*[\)\]]",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    cleaned = _TITLE_NOISE.sub("", title).strip(" -–—|")
    return cleaned if cleaned else title


async def _update_presence(track: Optional[Track], is_playing: bool = False) -> None:
    """Update the bot's Discord status.
    While playing: 'Listening to [song]' with artist + duration (Spotify-style).
    When idle: yellow idle dot with a prompt."""
    try:
        if track and is_playing:
            title  = _clean_title(track.title)
            # Extract artist — everything before the first ' - ' if present
            if " - " in title:
                artist, song = title.split(" - ", 1)
            else:
                artist, song = None, title

            dur_str = track.duration if track.duration not in ("?:??", "Unknown", None) else None

            # Build state line: artist • duration, or just whichever is available
            state_parts = []
            if artist:
                state_parts.append(artist.strip())
            if dur_str:
                state_parts.append(dur_str)
            state_line = " • ".join(state_parts) or None

            await bot.change_presence(
                status   = discord.Status.online,
                activity = discord.Activity(
                    type    = discord.ActivityType.listening,
                    name    = song.strip()[:128],
                    details = state_line,
                ),
            )
        else:
            await bot.change_presence(
                status   = discord.Status.idle,
                activity = discord.Activity(
                    type = discord.ActivityType.listening,
                    name = "nothing — use /play",
                ),
            )
    except Exception:
        pass


async def _start_playing(guild: discord.Guild, q: GuildQueue,
                          seek_secs: int = 0, send_np: bool = True) -> None:
    """Start audio playback for q.current. Resolves stream URL, builds FFmpeg source."""
    if not q.voice_client or q.current is None:
        return
    if not q.voice_client.is_connected():
        print("[Player] Voice client not connected — aborting playback")
        q.current = None
        return

    if not await _resolve_stream(q.current):
        failed_title = q.current.title
        print(f"[Player] Could not resolve stream for '{failed_title}' — skipping")
        if q.text_channel:
            try:
                await q.text_channel.send(
                    f"⚠️ Skipped **{failed_title}** — couldn't load the audio stream. "
                    f"(The video may be unavailable, age-restricted, or geo-blocked.)"
                )
            except Exception:
                pass
        await _play_next(guild)
        return

    source = _make_source(
        q.current, q.volume, seek_secs,
        bass         = q.bass_boost,
        audio_filter = q.audio_filter,
        eq_preset    = q.eq_preset,
        speed        = q.speed,
    )
    q.play_start = time.monotonic() - seek_secs
    q.paused_at  = None
    q.vote_skip_users.clear()

    if q.idle_task:
        q.idle_task.cancel()
        q.idle_task = None

    def after_play(error):
        if error:
            print(f"[Player] Playback error: {error!r}")
            err_str = str(error)
            if "4006" in err_str or "ConnectionClosed" in type(error).__name__:
                asyncio.run_coroutine_threadsafe(
                    _handle_voice_drop(guild), guild._state.loop
                )
                return
        asyncio.run_coroutine_threadsafe(_play_next(guild), guild._state.loop)

    q.voice_client.play(source, after=after_play)
    print(f"[Player] ▶ {q.current.title}")
    asyncio.create_task(_update_presence(q.current, is_playing=True))

    _cancel_np_tasks(q)

    if send_np and q.announce and q.text_channel and seek_secs == 0:
        try:
            view = NowPlayingView(guild.id)
            msg  = await q.text_channel.send(
                embed=_np_embed(q.current, q, q.play_start),
                view=view,
            )
            _register_np(q, msg, q.current, guild.id)
        except Exception as e:
            print(f"[Player] Could not send Now Playing card: {e}")


async def _play_next(guild: discord.Guild) -> None:
    """Advance the queue to the next track.
    Protected by a per-guild lock to prevent race conditions."""
    lock = get_play_lock(guild.id)
    if lock.locked():
        return
    async with lock:
        q = queues.get(guild.id)
        if q is None:
            return

        # Honour seek/restart requests
        if q.restart_current:
            q.restart_current = False
            seek   = q.seek_to
            q.seek_to = 0
            await _start_playing(guild, q, seek_secs=seek)
            return

        # Loop current song
        if q.loop_mode == "song" and q.current:
            await _start_playing(guild, q)
            return

        # Archive finished track
        if q.current:
            q.history.append(q.current)
            q.songs_played += 1

        # Loop queue
        if q.loop_mode == "queue" and q.current:
            q.tracks.append(q.current)

        # Queue exhausted — try autoplay before giving up
        if not q.tracks:
            if q.autoplay and q.current:
                print(f"[Autoplay] Finding related song for '{q.current.title}'…")
                history_urls = {t.webpage_url for t in q.history}
                autoplay_track = await fetch_autoplay_track(q.current.title, history_urls)
                if autoplay_track:
                    q.tracks.append(autoplay_track)
                    if q.text_channel:
                        try:
                            await q.text_channel.send(
                                f"🔄 **Autoplay:** Adding *{autoplay_track.title}*"
                            )
                        except Exception:
                            pass

        if not q.tracks:
            q.current = None
            asyncio.create_task(_update_presence(None))
            _cancel_np_tasks(q)
            if not q.mode_247 and q.voice_client and q.voice_client.is_connected():
                if q.idle_task:
                    q.idle_task.cancel()
                q.idle_task = asyncio.create_task(_idle_disconnect(guild))
            return

        q.current = q.tracks.popleft()
        await _start_playing(guild, q)


async def _handle_voice_drop(guild: discord.Guild) -> None:
    """Handle voice WebSocket disconnects. Attempts reconnect up to 3 times."""
    q = queues.get(guild.id)
    if not q:
        return

    channel = q.voice_client.channel if q.voice_client else None
    elapsed = int(time.monotonic() - q.play_start) if q.play_start else 0
    if q.current and q.current.duration_secs:
        elapsed = min(elapsed, q.current.duration_secs)

    print(f"[Voice] Drop detected in {guild.name} — attempting reconnect")
    _cancel_np_tasks(q)
    if q.idle_task:
        q.idle_task.cancel()
        q.idle_task = None

    reconnected = False
    for attempt in range(1, 4):
        await asyncio.sleep(5 * attempt)
        if not channel or not any(not m.bot for m in channel.members):
            print("[Voice] Channel empty — stopping reconnect")
            break
        try:
            existing = guild.voice_client
            if existing:
                try:
                    await asyncio.wait_for(existing.disconnect(force=True), timeout=5)
                except Exception:
                    pass
            vc = await asyncio.wait_for(channel.connect(reconnect=False), timeout=15)
            q.voice_client = vc
            reconnected    = True
            print(f"[Voice] Reconnected on attempt {attempt}")
            break
        except Exception as e:
            print(f"[Voice] Reconnect attempt {attempt} failed: {e}")

    if reconnected and q.current:
        q.restart_current = True
        q.seek_to         = elapsed
        asyncio.create_task(_play_next(guild))
        if q.text_channel:
            try:
                await q.text_channel.send("🔄 Reconnected — resuming playback.")
            except Exception:
                pass
    else:
        q.current    = None
        q.play_start = None
        asyncio.create_task(_update_presence(None))
        if q.text_channel:
            try:
                await q.text_channel.send("⚠️ Voice connection lost. Use `/play` to resume.")
            except Exception:
                pass


async def _idle_disconnect(guild: discord.Guild) -> None:
    """Disconnect after IDLE_TIMEOUT seconds of silence unless in 24/7 mode."""
    await asyncio.sleep(IDLE_TIMEOUT)
    q = queues.get(guild.id)
    if not q or q.mode_247:
        return
    if q.voice_client and q.voice_client.is_connected() and not q.voice_client.is_playing():
        ch_name      = q.voice_client.channel.name if q.voice_client.channel else "?"
        remaining    = list(q.tracks)
        song_count   = len(remaining)
        autosaved    = False

        # Auto-save remaining queue so nothing is lost
        if remaining:
            try:
                _save_playlist(guild.id, "autosave", remaining)
                autosaved = True
            except Exception:
                pass

        await q.voice_client.disconnect()
        text_ch = q.text_channel
        _cleanup_guild(guild.id)
        asyncio.create_task(_update_presence(None))
        print(f"[Auto-leave] Idle timeout in #{ch_name}")
        if text_ch:
            try:
                msg = "💤 Left the voice channel due to inactivity."
                if song_count > 0 and autosaved:
                    msg += (f"\n💾 **{song_count} song(s)** still in the queue were saved as "
                            f"playlist `autosave` — use `/playlist load autosave` to restore.")
                elif song_count > 0:
                    msg += f"\n⚠️ **{song_count} song(s)** were still in the queue."
                await text_ch.send(msg)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class MusicBot(commands.Bot):
    def __init__(self):
        intents              = discord.Intents.default()
        intents.voice_states = True
        intents.guilds       = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"📡 Commands synced to guild {GUILD_ID}")

    async def on_ready(self):
        print(f"🤖 Logged in as {self.user} (ID: {self.user.id})")
        try:
            await self.change_presence(
                status   = discord.Status.idle,
                activity = discord.Activity(
                    type = discord.ActivityType.listening,
                    name = "nothing — use /play",
                ),
            )
        except Exception:
            pass

    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after: discord.VoiceState):
        guild = member.guild

        if member.bot and member.id == self.user.id:
            if before.channel and not after.channel:
                q = queues.get(guild.id)
                if q and q.voice_client:
                    print(f"[Voice] Bot force-disconnected from #{before.channel.name}")
                    asyncio.create_task(_handle_voice_drop(guild))
            return

        vc = guild.voice_client
        if not vc or not vc.channel:
            return

        left_bots_channel = (
            before.channel == vc.channel and
            after.channel  != before.channel
        )
        if not left_bots_channel:
            return

        humans = [m for m in vc.channel.members if not m.bot]
        if humans:
            return

        q = queues.get(guild.id)
        if q and q.mode_247:
            return

        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()
        if q:
            _cancel_np_tasks(q)
            if q.idle_task:
                q.idle_task.cancel()
            q.current = None
        text_ch = q.text_channel if q else None
        _cleanup_guild(guild.id)
        asyncio.create_task(_update_presence(None))
        print(f"[Auto-leave] Everyone left #{before.channel.name}")
        if text_ch:
            try:
                await text_ch.send("👋 Everyone left — disconnected.")
            except Exception:
                pass


bot = MusicBot()


# ---------------------------------------------------------------------------
# Command check decorators
# ---------------------------------------------------------------------------

def music_channel_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != MUSIC_CHANNEL_ID:
            await interaction.response.send_message(
                f"⛔ Use music commands in <#{MUSIC_CHANNEL_ID}>.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def dj_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not has_dj_role(interaction.user):
            await interaction.response.send_message(
                f"❌ You need the **{DJ_ROLE_NAME}** role to use this.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


async def ensure_voice(interaction: discord.Interaction) -> Optional[GuildQueue]:
    """Connect the bot to the user's voice channel. All callers must defer() first."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ Join a voice channel first.", ephemeral=True)
        return None

    q       = get_queue(interaction.guild_id)
    channel = interaction.user.voice.channel
    existing_vc = interaction.guild.voice_client

    if (existing_vc and existing_vc.is_connected()
            and existing_vc.channel and existing_vc.channel != channel
            and (existing_vc.is_playing() or existing_vc.is_paused())):
        await interaction.followup.send(
            f"❌ I'm already playing in **{existing_vc.channel.name}**. Join that channel instead.",
            ephemeral=True,
        )
        return None

    needs_connect = (
        not existing_vc
        or not existing_vc.is_connected()
        or (existing_vc.channel != channel
            and not existing_vc.is_playing()
            and not existing_vc.is_paused())
    )

    if needs_connect:
        if existing_vc and not existing_vc.is_connected():
            try:
                await asyncio.wait_for(existing_vc.disconnect(force=True), timeout=5)
            except Exception:
                pass
        try:
            vc = await asyncio.wait_for(channel.connect(reconnect=True), timeout=20)
            q.voice_client = vc
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ Timed out joining your voice channel. Try again.", ephemeral=True)
            return None
        except Exception as e:
            print(f"[Voice] Failed to connect: {e}")
            await interaction.followup.send("❌ Couldn't connect to your voice channel.", ephemeral=True)
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
        opts = {**YDL_OPTS, "extract_flat": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{current}", download=False)
            return info.get("entries", [])

    try:
        entries = await asyncio.wait_for(loop.run_in_executor(None, _search), timeout=3)
    except Exception:
        return []

    choices = []
    for e in entries[:10]:
        title  = e.get("title", "Unknown")
        vid_id = e.get("id", "")
        if not vid_id:
            continue
        dur = e.get("duration")
        label = f"{title} ({fmt_dur(dur)})" if dur else title
        choices.append(
            app_commands.Choice(name=label[:100], value=f"https://youtu.be/{vid_id}"[:100])
        )
    return choices


# ---------------------------------------------------------------------------
# Slash commands — Playback
# ---------------------------------------------------------------------------

@bot.tree.command(name="play", description="Play a song — search by name, YouTube URL, Spotify URL, or playlist")
@app_commands.describe(query="Song name, YouTube link, Spotify link, or playlist URL")
@music_channel_only()
async def cmd_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    try:
        q = await ensure_voice(interaction)
        if q is None:
            return

        uid = interaction.user.id
        if SONG_LIMIT > 0 and user_song_count(q, uid) >= SONG_LIMIT:
            return await interaction.followup.send(
                f"❌ You already have **{SONG_LIMIT}** songs in the queue. Wait for one to finish.",
                ephemeral=True,
            )

        is_playlist = ("list=" in query) and query.startswith(("http://", "https://"))
        if is_playlist:
            tracks = await fetch_playlist(query, str(interaction.user), uid)
            if not tracks:
                return await interaction.followup.send("❌ Couldn't load that playlist.")
            start_now = q.current is None and not q.voice_client.is_playing()
            for t in tracks:
                q.tracks.append(t)
            if start_now and q.tracks:
                q.current = q.tracks.popleft()
                await _start_playing(interaction.guild, q)
            return await interaction.followup.send(
                f"{'▶️ Starting' if start_now else '➕ Added'} **{len(tracks)} songs** from the playlist."
            )

        track = await fetch_track(query, str(interaction.user), uid)
        if track is None:
            return await interaction.followup.send("❌ Couldn't find that song. Try a different search.")
        if isinstance(track, str):
            return await interaction.followup.send(track)

        if q.voice_client.is_playing() or q.voice_client.is_paused() or q.current is not None:
            q.tracks.append(track)
            await interaction.followup.send(embed=_queued_embed(track, q))
        else:
            q.current = track
            await _start_playing(interaction.guild, q, send_np=False)
            view = NowPlayingView(interaction.guild_id)
            msg  = await interaction.followup.send(
                embed=_np_embed(q.current, q, q.play_start),
                view=view,
            )
            _register_np(q, msg, track, interaction.guild_id)

    except Exception as e:
        print(f"[cmd_play] Unhandled error: {e}")
        traceback.print_exc()
        try:
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
        except Exception:
            pass


@cmd_play.autocomplete("query")
async def play_autocomplete(interaction: discord.Interaction, current: str):
    if not current or len(current) < 2:
        return []
    return await _search_suggestions(current)


@bot.tree.command(name="playnext", description="Queue a song to play right after the current one")
@app_commands.describe(query="Song name or YouTube / Spotify URL")
@music_channel_only()
@dj_only()
async def cmd_playnext(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = await ensure_voice(interaction)
    if q is None:
        return
    track = await fetch_track(query, str(interaction.user), interaction.user.id)
    if track is None:
        return await interaction.followup.send("❌ Couldn't find that song.")
    if isinstance(track, str):
        return await interaction.followup.send(track)
    q.tracks.appendleft(track)
    embed = discord.Embed(title="⏫ Playing Next",
                          description=f"**[{track.title}]({track.webpage_url})**",
                          color=0x5865F2)
    embed.add_field(name="Duration", value=track.duration, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    await interaction.followup.send(embed=embed)


@cmd_playnext.autocomplete("query")
async def playnext_autocomplete(interaction: discord.Interaction, current: str):
    if not current or len(current) < 2:
        return []
    return await _search_suggestions(current)


@bot.tree.command(name="search", description="Search YouTube and pick from 5 results")
@app_commands.describe(query="What to search for")
@music_channel_only()
async def cmd_search(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = await ensure_voice(interaction)
    if q is None:
        return
    entries = await search_youtube(query, count=5)
    if not entries:
        return await interaction.followup.send("❌ No results found. Try a different search.")
    lines = []
    for i, e in enumerate(entries[:5], 1):
        title  = e.get("title", "Unknown")
        dur    = e.get("duration")
        vid_id = e.get("id", "")
        url    = f"https://youtu.be/{vid_id}" if vid_id else ""
        lines.append(f"`{i}.` [{title}]({url}) `{fmt_dur(dur) if dur else '?'}`")
    embed = discord.Embed(title=f"🔍 Results for: {query}",
                          description="\n".join(lines), color=0x5865F2)
    embed.set_footer(text="Pick a song from the dropdown below")
    view = SearchView(entries[:5], interaction.guild, q, str(interaction.user), interaction.user.id)
    await interaction.followup.send(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Slash commands — Queue controls
# ---------------------------------------------------------------------------

@bot.tree.command(name="skip", description="Skip the current song (DJs skip instantly, others vote)")
@music_channel_only()
async def cmd_skip(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not (q.voice_client.is_playing() or q.voice_client.is_paused()):
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    if not _same_vc(interaction, q):
        return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)
    if has_dj_role(interaction.user):
        q.vote_skip_users.clear()
        q.voice_client.stop()
        return await interaction.response.send_message("⏭️ Skipped.")
    listeners = [m for m in q.voice_client.channel.members if not m.bot]
    needed    = max(1, math.ceil(len(listeners) / 2))
    if interaction.user.id in q.vote_skip_users:
        return await interaction.response.send_message("🗳️ You already voted to skip.", ephemeral=True)
    q.vote_skip_users.add(interaction.user.id)
    valid_votes = sum(1 for uid in q.vote_skip_users if any(m.id == uid for m in listeners))
    if valid_votes >= needed:
        q.vote_skip_users.clear()
        q.voice_client.stop()
        await interaction.response.send_message(f"⏭️ Vote skip passed! ({valid_votes}/{needed})")
    else:
        await interaction.response.send_message(
            f"🗳️ Skip vote: **{valid_votes}/{needed}** — need {needed - valid_votes} more.")


@bot.tree.command(name="skipto", description="Skip to a specific position in the queue")
@app_commands.describe(position="Queue position to jump to")
@music_channel_only()
@dj_only()
async def cmd_skipto(interaction: discord.Interaction, position: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        return await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
    if position < 1 or position > len(q.tracks):
        return await interaction.response.send_message(f"❌ Position must be 1–{len(q.tracks)}.", ephemeral=True)
    lst = list(q.tracks)
    for t in lst[:position - 1]:
        q.history.append(t)
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
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    secs = parse_time(timestamp)
    if secs is None:
        return await interaction.response.send_message("❌ Invalid time. Use `1:30` or `90`.", ephemeral=True)
    dur = q.current.duration_secs or 0
    if dur and secs >= dur:
        return await interaction.response.send_message(f"❌ Song is only {q.current.duration} long.", ephemeral=True)
    q.restart_current = True
    q.seek_to         = secs
    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()
    await interaction.response.send_message(f"⏩ Seeking to **{fmt_dur(secs)}**…")


@bot.tree.command(name="stop", description="Stop playback and disconnect the bot")
@music_channel_only()
@dj_only()
async def cmd_stop(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    _cancel_np_tasks(q)
    if q.idle_task:
        q.idle_task.cancel()
        q.idle_task = None
    q.tracks.clear()
    q.current         = None
    q.restart_current = False
    if q.voice_client.is_playing() or q.voice_client.is_paused():
        q.voice_client.stop()
    await q.voice_client.disconnect()
    _cleanup_guild(interaction.guild_id)
    asyncio.create_task(_update_presence(None))
    await interaction.response.send_message("⏹️ Stopped and disconnected.")


@bot.tree.command(name="pause", description="Pause the current song")
@music_channel_only()
@dj_only()
async def cmd_pause(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    q.voice_client.pause()
    q.paused_at = time.monotonic()
    await interaction.response.send_message("⏸️ Paused.")


@bot.tree.command(name="resume", description="Resume the paused song")
@music_channel_only()
@dj_only()
async def cmd_resume(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client or not q.voice_client.is_paused():
        return await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
    if q.paused_at and q.play_start:
        q.play_start += time.monotonic() - q.paused_at
    q.paused_at = None
    q.voice_client.resume()
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="replay", description="Restart the current song from the beginning")
@music_channel_only()
@dj_only()
async def cmd_replay(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.current:
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    q.restart_current = True
    q.seek_to         = 0
    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()
    await interaction.response.send_message("🔄 Restarting from the beginning.")


@bot.tree.command(name="loop", description="Cycle loop mode: Off → Song → Queue → Off")
@music_channel_only()
@dj_only()
async def cmd_loop(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    modes   = ["off", "song", "queue"]
    q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % len(modes)]
    labels  = {"off": "🔁 Loop is now **Off**", "song": "🔂 Looping the **current song**",
               "queue": "🔁 Looping the **entire queue**"}
    await interaction.response.send_message(labels[q.loop_mode])


@bot.tree.command(name="shuffle", description="Shuffle the upcoming queue")
@music_channel_only()
@dj_only()
async def cmd_shuffle(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        return await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
    lst = list(q.tracks)
    random.shuffle(lst)
    q.tracks = deque(lst)
    await interaction.response.send_message(f"🔀 Shuffled **{len(lst)}** songs.")


@bot.tree.command(name="clear", description="Clear all upcoming songs without stopping the current one")
@music_channel_only()
@dj_only()
async def cmd_clear(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        return await interaction.response.send_message("❌ The queue is already empty.", ephemeral=True)
    count = len(q.tracks)
    q.tracks.clear()
    await interaction.response.send_message(f"🗑️ Cleared **{count}** song(s).")


@bot.tree.command(name="remove", description="Remove a song from the queue by position")
@app_commands.describe(position="Position number (see /queue)")
@music_channel_only()
@dj_only()
async def cmd_remove(interaction: discord.Interaction, position: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        return await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
    if position < 1 or position > len(q.tracks):
        return await interaction.response.send_message(f"❌ Position must be 1–{len(q.tracks)}.", ephemeral=True)
    lst     = list(q.tracks)
    removed = lst.pop(position - 1)
    q.tracks = deque(lst)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}**.")


@bot.tree.command(name="move", description="Move a song to a different position in the queue")
@app_commands.describe(from_pos="Current position", to_pos="New position")
@music_channel_only()
@dj_only()
async def cmd_move(interaction: discord.Interaction, from_pos: int, to_pos: int):
    q = queues.get(interaction.guild_id)
    if not q or not q.tracks:
        return await interaction.response.send_message("❌ The queue is empty.", ephemeral=True)
    n = len(q.tracks)
    if not (1 <= from_pos <= n) or not (1 <= to_pos <= n):
        return await interaction.response.send_message(f"❌ Positions must be between 1 and {n}.", ephemeral=True)
    lst = list(q.tracks)
    t   = lst.pop(from_pos - 1)
    lst.insert(to_pos - 1, t)
    q.tracks = deque(lst)
    await interaction.response.send_message(f"↕️ Moved **{t.title}** to position **{to_pos}**.")


# ---------------------------------------------------------------------------
# Slash commands — Audio effects
# ---------------------------------------------------------------------------

@bot.tree.command(name="filter", description="Apply an audio filter (nightcore, vaporwave, 8d, slowed, karaoke, reverb, off)")
@app_commands.describe(preset="Filter to apply — use 'off' to remove")
@app_commands.choices(preset=[
    app_commands.Choice(name="🌙 Nightcore (faster + higher pitch)", value="nightcore"),
    app_commands.Choice(name="🌊 Vaporwave (slower + lower pitch)",  value="vaporwave"),
    app_commands.Choice(name="🐢 Slowed (slightly slower/lower)",    value="slowed"),
    app_commands.Choice(name="🎧 8D Audio (panning effect)",         value="8d"),
    app_commands.Choice(name="🎤 Karaoke (remove vocals)",           value="karaoke"),
    app_commands.Choice(name="🔊 Reverb (echo effect)",              value="reverb"),
    app_commands.Choice(name="❌ Off (remove all filters)",           value="off"),
])
@music_channel_only()
@dj_only()
async def cmd_filter(interaction: discord.Interaction, preset: str):
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    if not _same_vc(interaction, q):
        return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)

    speed_reset_note = ""
    if preset != "off" and abs(q.speed - 1.0) > 0.01:
        q.speed          = 1.0
        speed_reset_note = "\n⚠️ Your speed setting was reset to 1.0× (filters handle tempo internally)."

    q.audio_filter = None if preset == "off" else preset
    if q.voice_client and q.current and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()
    else:
        asyncio.create_task(_refresh_np_embed(q))

    label = "❌ Filter removed." if preset == "off" else f"✅ Filter set to **{preset}**."
    await interaction.response.send_message(label + speed_reset_note)


@bot.tree.command(name="eq", description="Apply an equalizer preset (flat, bass, treble, pop, rock, jazz, classical)")
@app_commands.describe(preset="EQ preset to apply")
@app_commands.choices(preset=[
    app_commands.Choice(name="Flat (no EQ)",          value="flat"),
    app_commands.Choice(name="🔊 Bass Boost",         value="bass"),
    app_commands.Choice(name="✨ Treble Boost",       value="treble"),
    app_commands.Choice(name="🎵 Pop",                value="pop"),
    app_commands.Choice(name="🎸 Rock",               value="rock"),
    app_commands.Choice(name="🎷 Jazz",               value="jazz"),
    app_commands.Choice(name="🎻 Classical",          value="classical"),
])
@music_channel_only()
@dj_only()
async def cmd_eq(interaction: discord.Interaction, preset: str):
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    if not _same_vc(interaction, q):
        return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)
    q.eq_preset = preset
    if q.voice_client and q.current and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()
    else:
        asyncio.create_task(_refresh_np_embed(q))
    await interaction.response.send_message(f"🎚 EQ set to **{preset}**.")


@bot.tree.command(name="speed", description="Set playback speed (0.25–3.0×) without pitch change")
@app_commands.describe(value="Speed multiplier — e.g. 1.5 for faster, 0.75 for slower, 1.0 to reset")
@music_channel_only()
@dj_only()
async def cmd_speed(interaction: discord.Interaction, value: float):
    if not 0.25 <= value <= 3.0:
        return await interaction.response.send_message("❌ Speed must be between **0.25** and **3.0**.", ephemeral=True)
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    if not _same_vc(interaction, q):
        return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)
    if q.audio_filter:
        return await interaction.response.send_message(
            "❌ Clear the current audio filter first (`/filter off`) before setting speed.", ephemeral=True)
    q.speed = value
    if q.voice_client and q.current and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()
    else:
        asyncio.create_task(_refresh_np_embed(q))
    state = "✅ Speed reset to normal." if abs(value - 1.0) < 0.01 else f"⏩ Speed set to **{value:.2f}×**."
    await interaction.response.send_message(state)


# ---------------------------------------------------------------------------
# Slash commands — DJ settings
# ---------------------------------------------------------------------------

@bot.tree.command(name="volume", description="Set the volume (1–100) — applies immediately")
@app_commands.describe(level="Volume level between 1 and 100")
@music_channel_only()
@dj_only()
async def cmd_volume(interaction: discord.Interaction, level: int):
    if not 1 <= level <= 100:
        return await interaction.response.send_message("❌ Volume must be between 1 and 100.", ephemeral=True)
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    old      = q.volume
    q.volume = level / 100
    if (q.voice_client and q.current
            and (q.voice_client.is_playing() or q.voice_client.is_paused())
            and abs(old - q.volume) > 0.005):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")


@bot.tree.command(name="bass", description="Toggle bass boost on/off")
@music_channel_only()
@dj_only()
async def cmd_bass(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    if not _same_vc(interaction, q):
        return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)
    q.bass_boost = not q.bass_boost
    if q.voice_client and q.current and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        elapsed           = int(time.monotonic() - q.play_start) if q.play_start else 0
        q.restart_current = True
        q.seek_to         = elapsed
        q.voice_client.stop()
    else:
        asyncio.create_task(_refresh_np_embed(q))
    state = "🔊 **Bass boost ON**" if q.bass_boost else "🔈 **Bass boost OFF**"
    await interaction.response.send_message(state)


@bot.tree.command(name="autoplay", description="Toggle autoplay — keeps music going when the queue empties")
@music_channel_only()
@dj_only()
async def cmd_autoplay(interaction: discord.Interaction):
    q           = get_queue(interaction.guild_id)
    q.autoplay  = not q.autoplay
    state = (
        "🔄 **Autoplay ON** — I'll find related songs when the queue empties."
        if q.autoplay
        else "⏹️ **Autoplay OFF**."
    )
    await interaction.response.send_message(state)


@bot.tree.command(name="247", description="Toggle 24/7 mode — bot stays in channel even when idle")
@music_channel_only()
@dj_only()
async def cmd_247(interaction: discord.Interaction):
    q          = get_queue(interaction.guild_id)
    q.mode_247 = not q.mode_247
    state = (
        "✅ **24/7 mode ON** — I'll stay even when nothing is playing."
        if q.mode_247
        else "❌ **24/7 mode OFF** — I'll leave after 5 minutes of silence."
    )
    await interaction.response.send_message(state)


@bot.tree.command(name="announce", description="Toggle Now Playing cards between songs")
@music_channel_only()
@dj_only()
async def cmd_announce(interaction: discord.Interaction):
    q          = get_queue(interaction.guild_id)
    q.announce = not q.announce
    state = "✅ Now Playing cards **enabled**." if q.announce else "🔕 Now Playing cards **disabled**."
    await interaction.response.send_message(state)


# ---------------------------------------------------------------------------
# Slash commands — Info
# ---------------------------------------------------------------------------

@bot.tree.command(name="queue", description="Show the current queue")
@music_channel_only()
async def cmd_queue(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or (q.current is None and not q.tracks):
        return await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
    view = QueueView(q)
    await interaction.response.send_message(embed=view._build_embed(), view=view)


@bot.tree.command(name="nowplaying", description="Show what's currently playing with a live progress bar")
@music_channel_only()
async def cmd_nowplaying(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or q.current is None:
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    view = NowPlayingView(interaction.guild_id)
    await interaction.response.send_message(
        embed=_np_embed(q.current, q, q.play_start), view=view)


@bot.tree.command(name="lyrics", description="Show lyrics for the current song")
@music_channel_only()
async def cmd_lyrics(interaction: discord.Interaction):
    await interaction.response.defer()
    q = queues.get(interaction.guild_id)
    if not q or not q.current:
        return await interaction.followup.send("❌ Nothing is playing.", ephemeral=True)

    title     = q.current.title
    thumbnail = q.current.thumbnail
    lyrics    = await fetch_lyrics(title)
    if not lyrics:
        return await interaction.followup.send(
            f"❌ Couldn't find lyrics for **{title}**. Try searching manually on Genius."
        )

    # Split into pages of ≤ 1800 chars each
    chunks = []
    while len(lyrics) > 1800:
        split_at = lyrics.rfind("\n", 0, 1800)
        if split_at == -1:
            split_at = 1800
        chunks.append(lyrics[:split_at])
        lyrics = lyrics[split_at:].lstrip("\n")
    if lyrics:
        chunks.append(lyrics)

    clean = _clean_title(title)
    if len(chunks) == 1:
        embed = discord.Embed(title=f"📜 {clean}", description=chunks[0], color=0x5865F2)
        embed.set_footer(text="Powered by lyrics.ovh")
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        await interaction.followup.send(embed=embed)
    else:
        view = LyricsView(chunks, clean, thumbnail=thumbnail)
        await interaction.followup.send(embed=view._build_embed(), view=view)


@bot.tree.command(name="history", description="Show the last 10 songs played")
@music_channel_only()
async def cmd_history(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.history:
        return await interaction.response.send_message("📭 No history yet.", ephemeral=True)
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
    h, rem = divmod(uptime_secs, 3600)
    m, s   = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    q            = queues.get(interaction.guild_id)
    songs_played = q.songs_played if q else 0
    in_queue     = len(q.tracks)  if q else 0
    current      = q.current.title if q and q.current else "Nothing"
    mode_247     = "On"  if (q and q.mode_247)   else "Off"
    bass         = "On"  if (q and q.bass_boost)  else "Off"
    vol          = f"{int((q.volume if q else 1.0) * 100)}%"
    loop         = LOOP_LABELS.get(q.loop_mode if q else "off", "Off")
    autoplay     = "On"  if (q and q.autoplay)    else "Off"
    filt         = (q.audio_filter or "None").title() if q else "None"
    eq           = (q.eq_preset if q else "flat").title()
    speed        = f"{q.speed:.2f}×" if q else "1.00×"

    embed = discord.Embed(title="📊 Bot Stats", color=0x5865F2)
    embed.add_field(name="Uptime",       value=uptime_str,        inline=True)
    embed.add_field(name="Songs Played", value=str(songs_played), inline=True)
    embed.add_field(name="In Queue",     value=str(in_queue),     inline=True)
    embed.add_field(name="Now Playing",  value=current,           inline=False)
    embed.add_field(name="Volume",       value=vol,               inline=True)
    embed.add_field(name="Bass Boost",   value=bass,              inline=True)
    embed.add_field(name="Loop",         value=loop,              inline=True)
    embed.add_field(name="Filter",       value=filt,              inline=True)
    embed.add_field(name="EQ",           value=eq,                inline=True)
    embed.add_field(name="Speed",        value=speed,             inline=True)
    embed.add_field(name="24/7 Mode",    value=mode_247,          inline=True)
    embed.add_field(name="Autoplay",     value=autoplay,          inline=True)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Slash commands — Saved playlists
# ---------------------------------------------------------------------------

@bot.tree.command(name="playlist", description="Save, load, list, or delete server playlists")
@app_commands.describe(
    action="Action to perform",
    name="Playlist name",
)
@app_commands.choices(action=[
    app_commands.Choice(name="💾 Save current queue as a playlist", value="save"),
    app_commands.Choice(name="▶️ Load a playlist into the queue",   value="load"),
    app_commands.Choice(name="📋 List all saved playlists",         value="list"),
    app_commands.Choice(name="🗑️ Delete a saved playlist",          value="delete"),
])
@music_channel_only()
@dj_only()
async def cmd_playlist(interaction: discord.Interaction,
                        action: str, name: Optional[str] = None):
    guild_id = interaction.guild_id

    if action == "list":
        playlists = _list_playlists(guild_id)
        if not playlists:
            return await interaction.response.send_message("📭 No saved playlists yet.", ephemeral=True)
        embed = discord.Embed(title="📋 Saved Playlists", color=0x5865F2,
                              description="\n".join(f"`{i}.` {p}" for i, p in enumerate(playlists, 1)))
        return await interaction.response.send_message(embed=embed)

    if not name:
        return await interaction.response.send_message("❌ Provide a playlist name.", ephemeral=True)
    # Sanitise name — only alphanumeric, dash, underscore
    name = re.sub(r"[^\w\-]", "_", name.strip())[:32]
    if not name:
        return await interaction.response.send_message("❌ Invalid playlist name.", ephemeral=True)

    if action == "save":
        q = queues.get(guild_id)
        tracks_to_save: list[Track] = []
        if q and q.current:
            tracks_to_save.append(q.current)
        if q:
            tracks_to_save.extend(list(q.tracks))
        if not tracks_to_save:
            return await interaction.response.send_message("❌ Nothing in the queue to save.", ephemeral=True)
        _save_playlist(guild_id, name, tracks_to_save)
        await interaction.response.send_message(
            f"💾 Saved **{len(tracks_to_save)} song(s)** as playlist **{name}**."
        )

    elif action == "load":
        await interaction.response.defer()
        tracks = _load_playlist(guild_id, name)
        if tracks is None:
            return await interaction.followup.send(f"❌ No playlist named **{name}** found.", ephemeral=True)
        q = await ensure_voice(interaction)
        if q is None:
            return
        start_now = q.current is None and not q.voice_client.is_playing()
        for t in tracks:
            t.requested_by    = str(interaction.user)
            t.requested_by_id = interaction.user.id
            q.tracks.append(t)
        if start_now and q.tracks:
            q.current = q.tracks.popleft()
            await _start_playing(interaction.guild, q)
        await interaction.followup.send(
            f"{'▶️ Starting' if start_now else '➕ Loaded'} **{len(tracks)} songs** from playlist **{name}**."
        )

    elif action == "delete":
        if _delete_playlist(guild_id, name):
            await interaction.response.send_message(f"🗑️ Deleted playlist **{name}**.")
        else:
            await interaction.response.send_message(f"❌ No playlist named **{name}** found.", ephemeral=True)


@cmd_playlist.autocomplete("name")
async def playlist_name_autocomplete(interaction: discord.Interaction,
                                      current: str) -> list[app_commands.Choice[str]]:
    """Suggest saved playlist names when the user starts typing a name."""
    playlists = _list_playlists(interaction.guild_id)
    return [
        app_commands.Choice(name=p, value=p)
        for p in playlists
        if current.lower() in p.lower()
    ][:25]


# ---------------------------------------------------------------------------
# Slash commands — Extras (/np, /disconnect, /clearhistory, /update, /back, /ping, /effects)
# ---------------------------------------------------------------------------

@bot.tree.command(name="np", description="Show what's currently playing — shortcut for /nowplaying")
@music_channel_only()
async def cmd_np(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or q.current is None:
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    view = NowPlayingView(interaction.guild_id)
    await interaction.response.send_message(embed=_np_embed(q.current, q, q.play_start), view=view)


@bot.tree.command(name="disconnect", description="Stop playback and disconnect (alias for /stop)")
@music_channel_only()
@dj_only()
async def cmd_disconnect(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.voice_client:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)
    _cancel_np_tasks(q)
    if q.idle_task:
        q.idle_task.cancel()
        q.idle_task = None
    q.tracks.clear()
    q.current         = None
    q.restart_current = False
    if q.voice_client.is_playing() or q.voice_client.is_paused():
        q.voice_client.stop()
    await q.voice_client.disconnect()
    _cleanup_guild(interaction.guild_id)
    asyncio.create_task(_update_presence(None))
    await interaction.response.send_message("⏹️ Stopped and disconnected.")


@bot.tree.command(name="clearhistory", description="Clear the recently played history")
@music_channel_only()
@dj_only()
async def cmd_clearhistory(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.history:
        return await interaction.response.send_message("📭 No history to clear.", ephemeral=True)
    count = len(q.history)
    q.history.clear()
    await interaction.response.send_message(f"🗑️ Cleared **{count}** song(s) from history.")


@bot.tree.command(name="update", description="Update yt-dlp to the latest version — fixes YouTube issues")
@music_channel_only()
@dj_only()
async def cmd_update(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", "-U", "yt-dlp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = (stdout or b"").decode().strip()

        if "Successfully installed" in output:
            match = re.search(r"yt-dlp-([\d\.]+)", output)
            version = match.group(1) if match else "latest"
            await interaction.followup.send(
                f"✅ yt-dlp updated to **{version}**. Restart the bot to apply the update."
            )
        elif "already" in output.lower():
            import yt_dlp as _yt
            current_ver = getattr(_yt.version, "__version__", "unknown")
            await interaction.followup.send(f"✅ yt-dlp is already up to date (v{current_ver}).")
        else:
            snippet = output[:400] if output else "No output."
            await interaction.followup.send(f"✅ Update ran.\n```\n{snippet}\n```")
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ Update timed out after 60 seconds.")
    except Exception as e:
        await interaction.followup.send(f"❌ Update failed: `{e}`")


@bot.tree.command(name="back", description="Go back to the previous song")
@music_channel_only()
@dj_only()
async def cmd_back(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q or not q.history:
        return await interaction.response.send_message("📭 No previous song in history.", ephemeral=True)
    if not _same_vc(interaction, q):
        return await interaction.response.send_message("❌ Join my voice channel first.", ephemeral=True)

    prev = q.history.pop()
    # Put current song back at the front of the queue so it plays after
    if q.current:
        q.tracks.appendleft(q.current)
    q.current = prev

    if q.voice_client and (q.voice_client.is_playing() or q.voice_client.is_paused()):
        q.voice_client.stop()
        await interaction.response.send_message(f"⏮️ Going back to **{prev.title}**.")
    elif q.voice_client and q.voice_client.is_connected():
        await _start_playing(interaction.guild, q)
        await interaction.response.send_message(f"⏮️ Playing **{prev.title}**.")
    else:
        await interaction.response.send_message("❌ Not connected to a voice channel.", ephemeral=True)


@bot.tree.command(name="ping", description="Check the bot's response latency")
async def cmd_ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    if latency_ms < 100:
        indicator = "🟢"
    elif latency_ms < 250:
        indicator = "🟡"
    else:
        indicator = "🔴"
    await interaction.response.send_message(
        f"{indicator} **Pong!** Latency: `{latency_ms} ms`", ephemeral=True
    )


@bot.tree.command(name="effects", description="Show all currently active audio effects at a glance")
@music_channel_only()
async def cmd_effects(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id)
    if not q:
        return await interaction.response.send_message("❌ Not connected.", ephemeral=True)

    filt  = q.audio_filter.title() if q.audio_filter else "None"
    eq    = q.eq_preset.title()
    speed = f"{q.speed:.2f}×" if abs(q.speed - 1.0) > 0.01 else "Normal (1.00×)"
    bass  = "On 🔊" if q.bass_boost else "Off"
    vol   = f"{int(q.volume * 100)}%"

    embed = discord.Embed(title="🎚 Active Audio Effects", color=0x5865F2)
    embed.add_field(name="Filter",     value=filt,  inline=True)
    embed.add_field(name="EQ Preset",  value=eq,    inline=True)
    embed.add_field(name="Speed",      value=speed, inline=True)
    embed.add_field(name="Bass Boost", value=bass,  inline=True)
    embed.add_field(name="Volume",     value=vol,   inline=True)
    if q.current:
        embed.set_footer(text=f"Currently playing: {q.current.title}")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

@bot.tree.command(name="help", description="Show all commands and tips")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="🎵 Different Music — Commands", color=0x5865F2)
    embed.add_field(name="▶️ Playback",
        value=("`/play` — Search or paste a YouTube/Spotify URL or playlist\n"
               "`/playnext` — Queue a song right after the current one\n"
               "`/search` — Pick from a 5-result dropdown\n"
               "`/pause` `/resume` `/stop` `/replay`\n"
               "`/seek <time>` — Jump to e.g. `1:30` or `90`"), inline=False)
    embed.add_field(name="📋 Queue",
        value=("`/queue` — Paginated queue with per-song wait times\n"
               "`/nowplaying` — Live progress bar\n"
               "`/history` — Last 10 songs\n"
               "`/skipto <pos>` `/remove <pos>` `/move <from> <to>` `/clear`"), inline=False)
    embed.add_field(name="🎛️ Controls",
        value=("`/skip` — DJs skip instantly; others cast a vote\n"
               "`/loop` — Off → Song → Queue\n"
               "`/shuffle` `/volume <1-100>` `/bass`"), inline=False)
    embed.add_field(name="🎚 Audio Effects",
        value=("`/filter` — nightcore, vaporwave, slowed, 8d, karaoke, reverb\n"
               "`/eq` — flat, bass, treble, pop, rock, jazz, classical\n"
               "`/speed <0.25–3.0>` — change playback speed"), inline=False)
    embed.add_field(name="📂 Playlists",
        value=("`/playlist save <name>` — save the current queue\n"
               "`/playlist load <name>` — load a saved playlist\n"
               "`/playlist list` — show all saved playlists\n"
               "`/playlist delete <name>` — remove a saved playlist"), inline=False)
    embed.add_field(name="⚙️ Settings",
        value=("`/autoplay` — auto-queue related songs when queue empties\n"
               "`/247` — stay connected forever\n"
               "`/announce` — toggle Now Playing cards\n"
               "`/lyrics` — show lyrics for the current song\n"
               "`/np` — quick shortcut for /nowplaying\n"
               "`/back` — go back to the previous song\n"
               "`/disconnect` — stop and disconnect (alias for /stop)\n"
               "`/clearhistory` — wipe the recently played list\n"
               "`/effects` — see all active audio effects at a glance\n"
               "`/update` — update yt-dlp to fix YouTube issues\n"
               "`/ping` — check bot latency\n"
               "`/stats` — uptime and current settings"), inline=False)
    embed.add_field(name="💡 Tips",
        value=(f"• DJ role: **{DJ_ROLE_NAME}** (server owner & admins always bypass)\n"
               f"• Max **{SONG_LIMIT}** queued songs per person\n"
               "• Now Playing card auto-updates every 20 s\n"
               "• Paste a Spotify link and I'll find it on YouTube\n"
               "• Buttons on the Now Playing card: ⏸️ ⏭️ 🔁 🔀"), inline=False)
    embed.set_footer(text="Different Music Bot")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

bot.run(TOKEN)
