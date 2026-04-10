from __future__ import annotations

import asyncio
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

GUILD_ID         = 850386896509337710
MUSIC_CHANNEL_ID = 1487195424111726743

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": False,
    "default_search": "ytsearch",
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Track:
    title:       str
    webpage_url: str
    duration:    int
    thumbnail:   str
    requester:   discord.Member


class GuildMusicState:
    def __init__(self):
        self.queue:      deque[Track]       = deque()
        self.current:    Optional[Track]    = None
        self.loop_mode:  str                = "off"   # off | track | queue
        self.volume:     float              = 0.5
        self.np_msg:     Optional[discord.Message] = None
        self._skip_flag: bool               = False


# ── yt-dlp helpers ─────────────────────────────────────────────────────────────

def _ydl_extract(query: str) -> Optional[dict]:
    try:
        import yt_dlp
        opts = dict(YDL_OPTS)
        if not query.startswith("http"):
            query = f"ytsearch:{query}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                return {"entries": entries} if entries else None
            return info
    except Exception as e:
        print(f"[Music] ydl error: {e}")
        return None


def _ydl_refresh(webpage_url: str) -> Optional[str]:
    """Get a fresh stream URL for a track (avoids expiry)."""
    try:
        import yt_dlp
        opts = dict(YDL_OPTS)
        opts["noplaylist"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            return info.get("url")
    except Exception as e:
        print(f"[Music] refresh error: {e}")
        return None


async def _resolve(query: str, loop: asyncio.AbstractEventLoop) -> Optional[dict]:
    if "spotify.com/track" in query:
        sp = await _resolve_spotify(query)
        if not sp:
            return None
        query = sp
    elif "music.apple.com" in query:
        am = await _resolve_apple(query)
        if not am:
            return None
        query = am
    return await loop.run_in_executor(None, _ydl_extract, query)


async def _resolve_spotify(url: str) -> Optional[str]:
    cid = os.getenv("SPOTIFY_CLIENT_ID")
    cs  = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not cid or not cs:
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=cid, client_secret=cs))
        m = re.search(r"track/([A-Za-z0-9]+)", url)
        if not m:
            return None
        t = sp.track(m.group(1))
        return f"{t['artists'][0]['name']} - {t['name']}"
    except Exception as e:
        print(f"[Music] Spotify error: {e}")
        return None


async def _resolve_apple(url: str) -> Optional[str]:
    try:
        import aiohttp
        m = re.search(r"/(\d+)(?:\?|$)", url)
        if not m:
            return None
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://itunes.apple.com/lookup?id={m.group(1)}") as r:
                data = await r.json(content_type=None)
                if data.get("resultCount", 0) > 0:
                    res = data["results"][0]
                    return f"{res.get('artistName','')} - {res.get('trackName','')}"
    except Exception as e:
        print(f"[Music] Apple Music error: {e}")
    return None


def _fmt(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ── Now Playing buttons (Pi-compatible subclasses) ─────────────────────────────

class _PauseBtn(discord.ui.Button):
    def __init__(self, cog: "DiffMusic"):
        super().__init__(emoji="⏸", style=discord.ButtonStyle.secondary,
                         custom_id="diff_music:pause", row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            self.emoji = "▶️"
        elif vc and vc.is_paused():
            vc.resume()
            self.emoji = "⏸"
        await self.cog._refresh_np(interaction.guild)


class _SkipBtn(discord.ui.Button):
    def __init__(self, cog: "DiffMusic"):
        super().__init__(emoji="⏭", style=discord.ButtonStyle.secondary,
                         custom_id="diff_music:skip", row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state = self.cog._state(interaction.guild_id)
            state._skip_flag = True
            vc.stop()
        else:
            await interaction.followup.send("Nothing playing.", ephemeral=True)


class _StopBtn(discord.ui.Button):
    def __init__(self, cog: "DiffMusic"):
        super().__init__(emoji="⏹", style=discord.ButtonStyle.danger,
                         custom_id="diff_music:stop", row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        state = self.cog._state(interaction.guild_id)
        state.queue.clear()
        state._skip_flag = True
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        state.current = None
        await self.cog._clear_np(interaction.guild)


class _LoopBtn(discord.ui.Button):
    def __init__(self, cog: "DiffMusic"):
        super().__init__(emoji="🔁", style=discord.ButtonStyle.secondary,
                         custom_id="diff_music:loop", row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        state = self.cog._state(interaction.guild_id)
        modes = ["off", "track", "queue"]
        state.loop_mode = modes[(modes.index(state.loop_mode) + 1) % 3]
        labels = {"off": "🔁 Loop Off", "track": "🔂 Loop Track", "queue": "🔁 Loop Queue"}
        await interaction.followup.send(labels[state.loop_mode], ephemeral=True)
        await self.cog._refresh_np(interaction.guild)


class _ShuffleBtn(discord.ui.Button):
    def __init__(self, cog: "DiffMusic"):
        super().__init__(emoji="🔀", style=discord.ButtonStyle.secondary,
                         custom_id="diff_music:shuffle", row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        state = self.cog._state(interaction.guild_id)
        q = list(state.queue)
        random.shuffle(q)
        state.queue = deque(q)
        await interaction.followup.send("🔀 Queue shuffled!", ephemeral=True)
        await self.cog._refresh_np(interaction.guild)


class NowPlayingView(discord.ui.View):
    def __init__(self, cog: "DiffMusic"):
        super().__init__(timeout=None)
        self.add_item(_PauseBtn(cog))
        self.add_item(_SkipBtn(cog))
        self.add_item(_StopBtn(cog))
        self.add_item(_LoopBtn(cog))
        self.add_item(_ShuffleBtn(cog))


# ── Main Cog ───────────────────────────────────────────────────────────────────

class DiffMusic(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self._states: dict[int, GuildMusicState] = {}

    def _state(self, gid: int) -> GuildMusicState:
        if gid not in self._states:
            self._states[gid] = GuildMusicState()
        return self._states[gid]

    def _build_np_embed(self, state: GuildMusicState) -> discord.Embed:
        t = state.current
        if not t:
            return discord.Embed(title="Nothing playing", color=0x2F3136)
        loop_icons = {"off": "➡️ Off", "track": "🔂 Track", "queue": "🔁 Queue"}
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"**[{t.title}]({t.webpage_url})**",
            color=0x1DB954,
        )
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        embed.add_field(name="⏱ Duration",  value=_fmt(t.duration),              inline=True)
        embed.add_field(name="🔁 Loop",     value=loop_icons[state.loop_mode],    inline=True)
        embed.add_field(name="🔊 Volume",   value=f"{int(state.volume * 100)}%",  inline=True)
        embed.add_field(name="📋 Up Next",  value=f"{len(state.queue)} track(s)", inline=True)
        embed.set_footer(
            text=f"Requested by {t.requester.display_name}",
            icon_url=t.requester.display_avatar.url,
        )
        return embed

    async def _refresh_np(self, guild: discord.Guild):
        state = self._state(guild.id)
        if not state.np_msg:
            return
        try:
            await state.np_msg.edit(
                embed=self._build_np_embed(state),
                view=NowPlayingView(self),
            )
        except Exception:
            pass

    async def _clear_np(self, guild: discord.Guild):
        state = self._state(guild.id)
        if state.np_msg:
            try:
                await state.np_msg.delete()
            except Exception:
                pass
            state.np_msg = None

    async def _post_np(self, guild: discord.Guild):
        state = self._state(guild.id)
        ch = guild.get_channel(MUSIC_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return
        await self._clear_np(guild)
        try:
            state.np_msg = await ch.send(
                embed=self._build_np_embed(state),
                view=NowPlayingView(self),
            )
        except Exception as e:
            print(f"[Music] NP post error: {e}")

    def _after_track(self, guild: discord.Guild, error=None):
        if error:
            print(f"[Music] playback error: {error}")
        asyncio.run_coroutine_threadsafe(self._advance(guild), self.bot.loop)

    async def _advance(self, guild: discord.Guild):
        state = self._state(guild.id)

        if state._skip_flag:
            state._skip_flag = False
        else:
            if state.loop_mode == "track" and state.current:
                state.queue.appendleft(state.current)
            elif state.loop_mode == "queue" and state.current:
                state.queue.append(state.current)

        if not state.queue:
            state.current = None
            await self._clear_np(guild)
            await asyncio.sleep(30)
            vc = guild.voice_client
            if vc and not vc.is_playing():
                try:
                    await vc.disconnect()
                except Exception:
                    pass
            return

        track = state.queue.popleft()
        state.current = track

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            state.current = None
            return

        try:
            loop       = asyncio.get_event_loop()
            stream_url = await loop.run_in_executor(None, _ydl_refresh, track.webpage_url)
            if not stream_url:
                await self._advance(guild)
                return
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS),
                volume=state.volume,
            )
            vc.play(source, after=lambda e: self._after_track(guild, e))
            await self._post_np(guild)
        except Exception as e:
            print(f"[Music] advance error: {e}")
            await self._advance(guild)

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Play a song or playlist from YouTube, Spotify, or Apple Music")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(query="Song name, YouTube/Spotify/Apple Music URL")
    async def play(self, interaction: discord.Interaction, query: str):
        if interaction.channel_id != MUSIC_CHANNEL_ID:
            return await interaction.response.send_message(
                f"Use <#{MUSIC_CHANNEL_ID}> for music commands.", ephemeral=True)
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True)
        await interaction.response.defer()

        guild = interaction.guild
        loop  = asyncio.get_event_loop()
        info  = await _resolve(query, loop)
        if not info:
            return await interaction.followup.send("❌ Couldn't find that track.", ephemeral=True)

        entries = info.get("entries", [info]) or [info]
        state   = self._state(guild.id)
        added   = 0
        for entry in entries[:50]:
            if not entry:
                continue
            state.queue.append(Track(
                title       = entry.get("title", "Unknown"),
                webpage_url = entry.get("webpage_url") or entry.get("url", ""),
                duration    = int(entry.get("duration") or 0),
                thumbnail   = entry.get("thumbnail") or "",
                requester   = interaction.user,
            ))
            added += 1

        vc = guild.voice_client
        if not vc:
            try:
                vc = await interaction.user.voice.channel.connect()
            except Exception as e:
                return await interaction.followup.send(f"❌ Couldn't join voice: {e}", ephemeral=True)

        if added == 1:
            title = entries[0].get("title", "track")
            await interaction.followup.send(f"✅ Added **{title}** to the queue.")
        else:
            await interaction.followup.send(f"✅ Added **{added} tracks** to the queue.")

        if not vc.is_playing() and not vc.is_paused():
            await self._advance(guild)

    @app_commands.command(name="skip", description="Skip the current track")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        state = self._state(interaction.guild_id)
        state._skip_flag = True
        vc.stop()
        await interaction.response.send_message("⏭ Skipped.", ephemeral=True)

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume paused playback")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Not paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop music and clear the queue")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def stop(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        state.queue.clear()
        state._skip_flag = True
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        state.current = None
        await self._clear_np(interaction.guild)
        await interaction.response.send_message("⏹ Stopped and disconnected.", ephemeral=True)

    @app_commands.command(name="queue", description="Show the music queue")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def queue_cmd(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.current and not state.queue:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        embed = discord.Embed(title="🎵 Music Queue", color=0x1DB954)
        if state.current:
            embed.add_field(
                name="▶️ Now Playing",
                value=f"**{state.current.title}** `{_fmt(state.current.duration)}`",
                inline=False,
            )
        lines = [
            f"`{i}.` {t.title} `{_fmt(t.duration)}`"
            for i, t in enumerate(list(state.queue)[:15], 1)
        ]
        if lines:
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        if len(state.queue) > 15:
            embed.set_footer(text=f"+ {len(state.queue) - 15} more tracks")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="volume", description="Set playback volume (0–100)")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(level="Volume between 0 and 100")
    async def volume(self, interaction: discord.Interaction, level: int):
        if not 0 <= level <= 100:
            return await interaction.response.send_message("Volume must be 0–100.", ephemeral=True)
        state = self._state(interaction.guild_id)
        state.volume = level / 100
        vc = interaction.guild.voice_client
        if vc and hasattr(vc.source, "volume"):
            vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.", ephemeral=True)
        await self._refresh_np(interaction.guild)

    @app_commands.command(name="loop", description="Set loop mode")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off",        value="off"),
        app_commands.Choice(name="Loop Track", value="track"),
        app_commands.Choice(name="Loop Queue", value="queue"),
    ])
    @app_commands.describe(mode="Choose a loop mode")
    async def loop_cmd(self, interaction: discord.Interaction, mode: str):
        state = self._state(interaction.guild_id)
        state.loop_mode = mode
        icons = {"off": "➡️ Off", "track": "🔂 Track", "queue": "🔁 Queue"}
        await interaction.response.send_message(
            f"Loop set to **{icons[mode]}**.", ephemeral=True)
        await self._refresh_np(interaction.guild)

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def shuffle_cmd(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        q = list(state.queue)
        random.shuffle(q)
        state.queue = deque(q)
        await interaction.response.send_message("🔀 Queue shuffled!", ephemeral=True)
        await self._refresh_np(interaction.guild)

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def nowplaying(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.send_message(
            embed=self._build_np_embed(state), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DiffMusic(bot))
