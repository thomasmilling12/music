import importlib.util
import os
import sys
import tempfile
import unittest
import wave
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from discord.ext import commands


os.environ.setdefault("DISCORD_TOKEN", "test-token")
MODULE_PATH = Path(__file__).with_name("main.py")
SPEC = importlib.util.spec_from_file_location("discord_music_bot", MODULE_PATH)
bot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot

original_run = commands.Bot.run
commands.Bot.run = lambda self, token: None
try:
    SPEC.loader.exec_module(bot)
finally:
    commands.Bot.run = original_run


def make_track(**overrides):
    values = {
        "title": "Test song",
        "webpage_url": "https://youtu.be/test",
        "stream_url": "https://media.example/audio",
        "duration": "5:00",
        "duration_secs": 300,
        "thumbnail": "",
        "requested_by": "tester",
    }
    values.update(overrides)
    return bot.Track(**values)


class FakeSource:
    def __init__(self, source, **kwargs):
        self.source = source
        self.kwargs = kwargs

    def cleanup(self):
        pass


class PlaybackHelperTests(unittest.TestCase):
    def test_position_accounts_for_seek_speed_and_pause(self):
        q = bot.GuildQueue(
            current=make_track(),
            play_start=100.0,
            start_position=30,
            speed=1.5,
        )
        self.assertEqual(bot._playback_position(q, now=110.0), 45)

        q.paused_at = 106.0
        self.assertEqual(bot._playback_position(q, now=110.0), 39)

    def test_signed_url_deadline_uses_expiry_margin(self):
        url = "https://media.example/audio?expire=1900"
        with (
            patch.object(bot.time, "time", return_value=1000.0),
            patch.object(bot.time, "monotonic", return_value=500.0),
        ):
            self.assertEqual(bot._stream_deadline(url), 1100.0)

    def test_source_has_headroom_limiter_and_safe_headers(self):
        track = make_track(http_headers={
            "User-Agent": "Music Bot/1.0",
            "Referer": "https://www.youtube.com/",
            "Accept-Encoding": "gzip",
        })
        with patch.object(bot.discord, "FFmpegOpusAudio", FakeSource):
            source = bot._make_source(
                track,
                volume=1.0,
                bass=True,
                eq_preset="flat",
            )
        try:
            self.assertEqual(source.kwargs["bitrate"], 96)
            self.assertIn("volume=0.720", source.kwargs["options"])
            self.assertIn("alimiter=limit=0.95", source.kwargs["options"])
            self.assertIn("-user_agent", source.kwargs["before_options"])
            self.assertIn("-referer", source.kwargs["before_options"])
            self.assertNotIn("Accept-Encoding", source.kwargs["before_options"])
            self.assertIsInstance(source.stderr_capture, bot._BoundedStderr)
        finally:
            source.cleanup()

    def test_transition_stop_is_bound_to_active_source(self):
        voice = Mock()
        q = bot.GuildQueue(voice_client=voice, playback_id=7)

        bot._stop_for_transition(q)

        voice.stop.assert_called_once_with()
        self.assertEqual(q.manual_stop_ids, {7})
        self.assertEqual(q.transition_id, 1)

    def test_bounded_ffmpeg_capture_produces_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "tone.wav"
            with wave.open(str(audio_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(b"\x00\x00" * 4800)

            capture = bot._BoundedStderr(limit=1024)
            source = bot.discord.FFmpegOpusAudio(
                str(audio_path),
                bitrate=96,
                stderr=capture,
            )
            try:
                self.assertTrue(source.read())
            finally:
                source.cleanup()
            self.assertLessEqual(len(capture.snapshot()), 1024)

    def test_ffmpeg_capture_keeps_only_latest_bytes(self):
        capture = bot._BoundedStderr(limit=8)
        capture.write(b"123456")
        capture.write(b"7890")
        self.assertEqual(capture.snapshot(), b"34567890")


class PlaybackTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.queues.clear()
        bot._play_locks.clear()
        self.guild = Mock(id=123, name="Test guild")

    async def test_stale_callback_cannot_advance_active_source(self):
        current = make_track()
        q = bot.GuildQueue(current=current, playback_id=9)
        q.tracks.append(make_track(title="Next"))
        bot.queues[self.guild.id] = q

        await bot._play_next(self.guild, playback_id=8)

        self.assertIs(q.current, current)
        self.assertEqual(len(q.tracks), 1)

    async def test_stream_error_refreshes_and_resumes_current_track(self):
        current = make_track()
        q = bot.GuildQueue(
            current=current,
            playback_id=4,
            play_start=100.0,
            start_position=0,
        )
        bot.queues[self.guild.id] = q

        with (
            patch.object(bot, "_playback_position", return_value=30),
            patch.object(bot.asyncio, "sleep", new=AsyncMock()),
            patch.object(bot, "_start_playing", new=AsyncMock()) as restart,
        ):
            await bot._play_next(
                self.guild,
                playback_id=4,
                playback_error="403 Forbidden",
            )

        self.assertEqual(current.retry_count, 1)
        self.assertEqual(current.stream_url, "")
        restart.assert_awaited_once_with(self.guild, q, seek_secs=29)

    async def test_user_restart_supersedes_delayed_recovery(self):
        current = make_track()
        q = bot.GuildQueue(
            current=current,
            playback_id=4,
            play_start=100.0,
        )
        bot.queues[self.guild.id] = q

        async def request_seek(_delay):
            bot._request_restart(q, 77)

        with (
            patch.object(bot, "_playback_position", return_value=30),
            patch.object(bot.asyncio, "sleep", side_effect=request_seek),
            patch.object(bot, "_start_playing", new=AsyncMock()) as restart,
        ):
            await bot._play_next(
                self.guild,
                playback_id=4,
                playback_error="403 Forbidden",
            )

        restart.assert_awaited_once_with(self.guild, q, seek_secs=77)
        self.assertFalse(q.restart_current)

    async def test_user_restart_supersedes_start_retry_backoff(self):
        current = make_track()
        q = bot.GuildQueue(current=current, transition_id=0)
        bot.queues[self.guild.id] = q

        async def request_seek(_delay):
            bot._request_restart(q, 81)

        with (
            patch.object(bot.asyncio, "sleep", side_effect=request_seek),
            patch.object(bot, "_start_playing", new=AsyncMock()) as restart,
        ):
            await bot._retry_start(
                self.guild,
                q,
                current,
                seek_secs=20,
                send_np=True,
                transition_id=0,
                reason="test failure",
            )

        restart.assert_awaited_once_with(self.guild, q, seek_secs=81)
        self.assertFalse(q.restart_current)

    async def test_skip_during_start_retry_advances_without_callback(self):
        current = make_track()
        q = bot.GuildQueue(
            current=current,
            tracks=deque([make_track(title="Next")]),
            transition_id=0,
        )
        bot.queues[self.guild.id] = q

        async def request_skip(_delay):
            bot._stop_for_transition(q)

        with (
            patch.object(bot.asyncio, "sleep", side_effect=request_skip),
            patch.object(bot, "_play_next", new=AsyncMock()) as advance,
        ):
            await bot._retry_start(
                self.guild,
                q,
                current,
                seek_secs=20,
                send_np=True,
                transition_id=0,
                reason="test failure",
            )

        await __import__("asyncio").sleep(0)
        advance.assert_awaited_once_with(
            self.guild,
            intentional_advance=True,
            expected_current=current,
            expected_transition=1,
        )

    async def test_source_construction_failure_forces_bounded_advance(self):
        current = make_track(retry_count=bot.MAX_STREAM_RETRIES)
        voice = Mock()
        voice.is_connected.return_value = True
        q = bot.GuildQueue(current=current, voice_client=voice, transition_id=4)
        bot.queues[self.guild.id] = q

        with (
            patch.object(bot, "_resolve_stream", new=AsyncMock(return_value=True)),
            patch.object(bot, "_make_source", side_effect=OSError("no process slots")),
            patch.object(bot, "_play_next", new=AsyncMock()) as advance,
        ):
            await bot._start_playing(self.guild, q)
            await __import__("asyncio").sleep(0)

        advance.assert_awaited_once_with(
            self.guild,
            force_advance=True,
            expected_current=current,
            expected_transition=4,
        )

    async def test_voice_play_failure_forces_bounded_advance(self):
        current = make_track(retry_count=bot.MAX_STREAM_RETRIES)
        voice = Mock()
        voice.is_connected.return_value = True
        voice.is_playing.return_value = False
        voice.is_paused.return_value = False
        voice.play.side_effect = RuntimeError("voice player unavailable")
        q = bot.GuildQueue(current=current, voice_client=voice, transition_id=2)
        bot.queues[self.guild.id] = q

        with (
            patch.object(bot, "_resolve_stream", new=AsyncMock(return_value=True)),
            patch.object(bot, "_make_source", return_value=FakeSource("audio")),
            patch.object(bot, "_play_next", new=AsyncMock()) as advance,
        ):
            await bot._start_playing(self.guild, q)
            await __import__("asyncio").sleep(0)

        advance.assert_awaited_once_with(
            self.guild,
            force_advance=True,
            expected_current=current,
            expected_transition=2,
        )

    async def test_restart_restores_manual_pause(self):
        voice = Mock()
        voice.is_connected.return_value = True
        voice.is_playing.return_value = False
        voice.is_paused.return_value = False
        q = bot.GuildQueue(
            current=make_track(),
            voice_client=voice,
            pause_after_restart=True,
        )
        bot.queues[self.guild.id] = q

        with (
            patch.object(bot, "_resolve_stream", new=AsyncMock(return_value=True)),
            patch.object(bot, "_make_source", return_value=FakeSource("audio")),
            patch.object(bot, "_update_presence", new=AsyncMock()),
        ):
            await bot._start_playing(self.guild, q, seek_secs=55)

        voice.play.assert_called_once()
        voice.pause.assert_called_once_with()
        self.assertIsNotNone(q.paused_at)
        self.assertFalse(q.pause_after_restart)
        self.assertEqual(q.start_position, 55)

    async def test_voice_reconnect_preserves_paused_position(self):
        member = Mock(bot=False)
        channel = Mock(members=[member])
        old_voice = Mock(channel=channel)
        old_voice.is_paused.return_value = True
        old_voice.disconnect = AsyncMock()
        new_voice = Mock()
        channel.connect = AsyncMock(return_value=new_voice)
        self.guild.voice_client = old_voice

        q = bot.GuildQueue(
            current=make_track(),
            voice_client=old_voice,
            playback_id=5,
            play_start=100.0,
            paused_at=110.0,
        )
        bot.queues[self.guild.id] = q

        with (
            patch.object(bot, "_playback_position", return_value=10),
            patch.object(bot.asyncio, "sleep", new=AsyncMock()),
            patch.object(bot, "_play_next", new=AsyncMock()),
        ):
            await bot._handle_voice_drop(self.guild)

        self.assertIs(q.voice_client, new_voice)
        self.assertTrue(q.pause_after_restart)
        self.assertTrue(q.restart_current)
        self.assertEqual(q.seek_to, 10)
        self.assertEqual(q.playback_id, 6)
        self.assertFalse(q.reconnecting)

    async def test_intentional_restart_wins_over_failure_detection(self):
        q = bot.GuildQueue(
            current=make_track(),
            playback_id=2,
            restart_current=True,
            seek_to=42,
            manual_stop_ids={2},
        )
        bot.queues[self.guild.id] = q

        with patch.object(bot, "_start_playing", new=AsyncMock()) as restart:
            await bot._play_next(self.guild, playback_id=2)

        restart.assert_awaited_once_with(self.guild, q, seek_secs=42)
        self.assertFalse(q.restart_current)
        self.assertNotIn(2, q.manual_stop_ids)

    async def test_exhausted_failure_moves_to_next_without_archiving(self):
        failed = make_track(retry_count=bot.MAX_STREAM_RETRIES)
        next_track = make_track(title="Next")
        q = bot.GuildQueue(
            current=failed,
            tracks=deque([next_track]),
            playback_id=3,
            play_start=100.0,
        )
        bot.queues[self.guild.id] = q

        with (
            patch.object(bot, "_start_playing", new=AsyncMock()) as start_next,
        ):
            await bot._play_next(
                self.guild,
                playback_id=3,
                playback_error="I/O error",
            )

        self.assertIs(q.current, next_track)
        self.assertEqual(list(q.history), [])
        start_next.assert_awaited_once_with(self.guild, q)


if __name__ == "__main__":
    unittest.main()