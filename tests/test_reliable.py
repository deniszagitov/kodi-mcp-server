"""Проверки контракта MCP на поддельном Kodi, без домашнего оборудования."""

import asyncio
from collections import deque
import json
import unittest

from kodi_rpc import ConnectionLost, Kodi, KodiError, RPC, item, safe


class FakeRPC:
    def __init__(self):
        self.calls = []
        self.event_sequence = 0
        self.events = deque()
        self.speed = 1
        self.position = 30
        self.entries = [{"title": "Первый"}, {"title": "Второй"}]
        self.fail_queue = False
        self.playing = True

    async def connect(self):
        pass

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "Player.GetActivePlayers":
            return [{"playerid": 1, "type": "video"}] if self.playing else []
        if method == "Player.GetProperties":
            if "canpause" in params.get("properties", []):
                raise KodiError("🛑 Kodi v13.5 не объявляет свойство canpause")
            return {"speed": self.speed, "time": {"seconds": self.position}, "totaltime": {"minutes": 2}, "playlistid": 1,
                    "canseek": True, "audiostreams": [{"index": 0}, {"index": 2}], "subtitles": [{"index": 1}]}
        if method == "Player.GetItem":
            return {"item": {"title": "Проба", "type": "movie", "id": 42, "file": "https://example.org/a.mp4?token=private"}}
        if method == "Player.PlayPause":
            self.speed = 1 if params["play"] else 0
            return {"speed": self.speed}
        if method == "Player.Seek":
            self.position = params["value"]["time"]["seconds"]
        if method == "VideoLibrary.GetMovies":
            return {"movies": [{"movieid": 3, "title": "Имя"}, {"movieid": 4, "title": "Имя"}]}
        if method == "Playlist.GetItems":
            return {"items": self.entries, "limits": {"total": len(self.entries)}}
        if method.startswith("Playlist.") and self.fail_queue:
            raise KodiError("🛑 Kodi отклонил изменение очереди")
        if method == "Player.Stop":
            self.playing = False
        if method in {"GUI.GetProperties", "XBMC.GetInfoLabels"}:
            return {}
        return "OK"


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rpc = FakeRPC()
        self.kodi = Kodi(self.rpc)

    async def test_repeated_pause_never_resumes(self):
        for _ in range(2):
            result = await self.kodi.play_pause(False)
            self.assertTrue(result["verified"])
        self.assertEqual(self.rpc.speed, 0)
        self.assertEqual([p["play"] for m, p in self.rpc.calls if m == "Player.PlayPause"], [False, False])

    async def test_repeated_resume_never_pauses(self):
        self.rpc.speed = 0
        for _ in range(2):
            self.assertTrue((await self.kodi.play_pause(True))["verified"])
        self.assertEqual(self.rpc.speed, 1)

    async def test_ambiguous_search_does_not_play(self):
        result = await self.kodi.library_search("movie", "Имя")
        self.assertTrue(result["ambiguous"])
        self.assertEqual([x["movieid"] for x in result["matches"]], [3, 4])
        self.assertNotIn("Player.Open", [m for m, _ in self.rpc.calls])

    async def test_relative_and_absolute_seek(self):
        result = await self.kodi.seek("relative", -10)
        self.assertEqual(result["requested_seconds"], 20)
        result = await self.kodi.seek("absolute", 55)
        self.assertEqual(result["requested_seconds"], 55)

    async def test_seek_out_of_range_does_not_write(self):
        with self.assertRaises(ValueError):
            await self.kodi.seek("absolute", 500)
        self.assertNotIn("Player.Seek", [m for m, _ in self.rpc.calls])

    async def test_queue_error_not_reported_as_success_or_retried(self):
        self.rpc.fail_queue = True
        with self.assertRaises(KodiError):
            await self.kodi.queue("clear")
        self.assertEqual([m for m, _ in self.rpc.calls].count("Playlist.Clear"), 1)

    async def test_invalid_queue_position_does_not_write(self):
        with self.assertRaises(ValueError):
            await self.kodi.queue("remove", position=9)
        self.assertNotIn("Playlist.Remove", [m for m, _ in self.rpc.calls])

    async def test_tracks_validate_all_indexes_before_mutating(self):
        with self.assertRaises(ValueError):
            await self.kodi.tracks(audio_index=2, subtitle_index=9)
        self.assertNotIn("Player.SetAudioStream", [m for m, _ in self.rpc.calls])

    async def test_noncontiguous_audio_index_is_allowed(self):
        await self.kodi.tracks(audio_index=2)
        self.assertIn(("Player.SetAudioStream", {"playerid": 1, "stream": 2}), self.rpc.calls)

    async def test_open_ack_is_not_playback_evidence(self):
        self.rpc.playing = False
        result = await self.kodi.open("movie", 42, wait_seconds=2)
        self.assertTrue(result["acknowledged"])
        self.assertFalse(result["av_started"])
        self.assertFalse(result["progressing"])

    async def test_stop_verified_by_empty_active_players(self):
        self.assertEqual((await self.kodi.stop())["players"], [])

    async def test_signed_stream_urls_not_exposed(self):
        result = await self.kodi.state()
        self.assertEqual(result["players"][0]["item"]["file"], "https://example.org/a.mp4")

    async def test_state_uses_supported_player_properties(self):
        self.assertEqual((await self.kodi.state())["players"][0]["time_seconds"], 30)

    async def test_invalid_input_does_not_execute_builtin(self):
        with self.assertRaises(ValueError):
            await self.kodi.input("System.Exec")
        self.assertEqual(self.rpc.calls, [])


class ValidationTests(unittest.TestCase):
    def test_only_explicit_media_and_youtube_plugins(self):
        self.assertEqual(item("movie", 1), {"movieid": 1})
        for kind, value in [("file", "special://profile/secrets.xml"), ("file", "/etc/passwd"),
                            ("file", "plugin://other/addon"), ("movie", True), ("file", "https://user:password@example.org/v.mp4")]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                item(kind, value)

    def test_no_raw_remote_tcp(self):
        with self.assertRaises(ValueError):
            RPC("192.0.2.1")

    def test_secret_redaction(self):
        self.assertEqual(safe({"api_key": "secret", "file": "https://example.org/v?sig=secret"}),
                         {"api_key": "[скрыто]", "file": "https://example.org/v"})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_unicode_and_coalesced_event_response(self):
        async def serve(reader, writer):
            request = json.loads(await reader.read(4096))
            event = {"jsonrpc": "2.0", "method": "Player.OnAVStart", "params": {"data": {"title": "Ёж"}}}
            payload = (json.dumps(event, ensure_ascii=False) + json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}})).encode()
            cut = payload.index("Ё".encode()) + 1
            writer.write(payload[:cut]); await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(payload[cut:]); await writer.drain()
            await reader.read()
            writer.close()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        rpc = RPC(port=server.sockets[0].getsockname()[1], timeout=1)
        try:
            self.assertEqual(await rpc.call("JSONRPC.Ping"), {"ok": True})
            self.assertEqual(rpc.events[0]["method"], "Player.OnAVStart")
        finally:
            await rpc.close(); server.close(); await server.wait_closed()

    async def test_disconnect_never_replays_write_and_next_read_reconnects(self):
        requests = []
        async def serve(reader, writer):
            request = json.loads(await reader.read(4096)); requests.append(request["method"])
            if request["method"] == "Playlist.Add":
                writer.close(); return
            writer.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": "pong"}).encode())
            await writer.drain(); await reader.read(); writer.close()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        rpc = RPC(port=server.sockets[0].getsockname()[1], timeout=1)
        try:
            with self.assertRaises(ConnectionLost) as caught:
                await rpc.call("Playlist.Add", {"playlistid": 1})
            self.assertTrue(caught.exception.ambiguous)
            self.assertEqual(await rpc.call("JSONRPC.Ping"), "pong")
            self.assertEqual(requests, ["Playlist.Add", "JSONRPC.Ping"])
        finally:
            await rpc.close(); server.close(); await server.wait_closed()

    async def test_offline_is_explicit(self):
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close(); await server.wait_closed()
        rpc = RPC(port=port, timeout=0.5)
        with self.assertRaises(ConnectionLost) as caught:
            await rpc.call("JSONRPC.Ping")
        self.assertFalse(caught.exception.ambiguous)
