"""Локальный Kodi JSON-RPC с событиями и без повторов неоднозначных записей."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class KodiError(RuntimeError):
    """Безопасная ошибка без URL потоков и секретов из ответа Kodi."""


class ConnectionLost(KodiError):
    def __init__(self, ambiguous=False):
        self.ambiguous = ambiguous
        super().__init__("🛑 Связь с Kodi потеряна; результат отправленной операции неизвестен" if ambiguous
                         else "🛑 Kodi недоступен; проверь запуск и SSH-туннель")


class RPC:
    def __init__(self, host="127.0.0.1", port=9090, timeout=20):
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("🛑 Неаутентифицированный Kodi TCP разрешён только через loopback/SSH")
        self.host, self.port, self.timeout = host, port, timeout
        self.reader = self.writer = self.reader_task = None
        self.connect_lock = asyncio.Lock()
        self.pending = {}
        self.sequence = self.event_sequence = 0
        self.events = deque(maxlen=128)

    async def connect(self):
        async with self.connect_lock:
            if self.writer and not self.writer.is_closing():
                return
            try:
                self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
            except (OSError, TimeoutError) as error:
                raise ConnectionLost() from error
            self.reader_task = asyncio.create_task(self.receive())

    async def receive(self):
        decoder, utf8, buffer = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8")(), ""
        try:
            while chunk := await self.reader.read(65536):
                buffer += utf8.decode(chunk)
                if len(buffer) > 16 * 1024 * 1024:
                    raise ValueError("🛑 Слишком большой ответ Kodi")
                while buffer.strip():
                    buffer = buffer.lstrip()
                    try:
                        message, end = decoder.raw_decode(buffer)
                    except json.JSONDecodeError:
                        break
                    buffer = buffer[end:]
                    if "id" in message:
                        future = self.pending.get(message["id"])
                        if future and not future.done():
                            future.set_result(message)
                    elif "method" in message:
                        self.event_sequence += 1
                        self.events.append({"sequence": self.event_sequence, "method": message["method"], "params": message.get("params", {})})
        except (OSError, ValueError, asyncio.CancelledError):
            pass
        finally:
            if self.writer:
                self.writer.close()
            self.writer = None
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionLost(ambiguous=True))

    async def call(self, method, params=None):
        await self.connect()
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            self.writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            await self.writer.drain()
            response = await asyncio.wait_for(future, self.timeout)
            if "error" in response:
                raise KodiError(f"🛑 Kodi отклонил {method}: код {response['error'].get('code')}")
            return response.get("result")
        except (TimeoutError, OSError) as error:
            # Запись не повторяется: она могла уже изменить очередь/плеер.
            raise ConnectionLost(ambiguous=True) from error
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)


def seconds(value):
    return value.get("hours", 0) * 3600 + value.get("minutes", 0) * 60 + value.get("seconds", 0) + value.get("milliseconds", 0) / 1000


def safe(value):
    """Не возвращать подписанные URL и значения авторизации в MCP/логи."""
    if isinstance(value, dict):
        return {key: ("[скрыто]" if re.search(r"password|token|secret|api.?key|authorization", key, re.I) else safe(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        return urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
    return value


def item(kind, value):
    if kind in {"movie", "episode"}:
        if type(value) is not int or value < 0:
            raise ValueError("🛑 Нужен числовой ID из поиска Kodi")
        return {kind + "id": value}
    if kind != "file" or not isinstance(value, str) or len(value) > 4096:
        raise ValueError("🛑 Нужен movie/episode ID либо media file/plugin URL")
    url = urlsplit(value)
    if url.scheme == "plugin":
        if url.netloc != "plugin.video.youtube" or "@" in url.netloc:
            raise ValueError("🛑 Разрешено только дополнение YouTube")
    elif url.scheme in {"https", "http"}:
        if not url.hostname or url.username or url.password:
            raise ValueError("🛑 URL с учётными данными запрещён")
    elif url.scheme:
        raise ValueError("🛑 Неподдерживаемая схема медиа")
    else:
        roots = [Path(p).resolve() for p in os.environ.get("KODI_MEDIA_ROOTS", "/media:/mnt").split(":")]
        path = Path(value)
        if not path.is_absolute() or not any(path.resolve().is_relative_to(root) for root in roots):
            raise ValueError("🛑 Локальный файл вне разрешённых каталогов медиа")
        if path.suffix.lower() not in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".mp3", ".flac", ".ogg", ".wav", ".m4a", ".m4v", ".ts"}:
            raise ValueError("🛑 Разрешены только медиафайлы")
    return {"file": value}


PROPERTIES = ["type", "speed", "time", "totaltime", "percentage", "position", "playlistid", "canseek",
              "audiostreams", "currentaudiostream", "subtitles", "currentsubtitle", "subtitleenabled"]


class Kodi:
    def __init__(self, rpc):
        self.rpc = rpc
        self.mutation = asyncio.Lock()

    async def capabilities(self, method=None):
        version = await self.rpc.call("JSONRPC.Version")
        schema = await self.rpc.call("JSONRPC.Introspect", {"getdescriptions": False, "getmetadata": True})
        methods = schema.get("methods", {})
        if method is not None:
            if method not in methods:
                raise ValueError("🛑 Метод отсутствует в фактическом Kodi JSON-RPC")
            types, selected, pending = schema.get("types", {}), {}, [methods[method]]
            while pending and len(selected) < 100:
                node = pending.pop()
                if isinstance(node, list):
                    pending.extend(node)
                elif isinstance(node, dict):
                    for key, value in node.items():
                        if key == "$ref" and value in types and value not in selected:
                            selected[value] = types[value]
                            pending.append(types[value])
                        elif isinstance(value, (dict, list)):
                            pending.append(value)
            return {"version": version, "method": method, "schema": methods[method], "types": selected}
        return {"version": version, "methods": sorted(methods), "method_count": len(methods),
                "screenshot_supported": "GUI.TakeScreenshot" in methods,
                "transport": "loopback TCP; удалённый доступ только через аутентифицированный SSH"}

    async def library_search(self, media, title, limit=25):
        if media not in {"movie", "tvshow", "episode"} or not 1 <= limit <= 100 or not title.strip():
            raise ValueError("🛑 Проверь вид медиа, запрос и лимит 1..100")
        plural = {"movie": "Movies", "tvshow": "TVShows", "episode": "Episodes"}[media]
        result = await self.rpc.call("VideoLibrary.Get" + plural, {
            "filter": {"field": "title", "operator": "contains", "value": title},
            "properties": ["title", "year"] if media != "episode" else ["title", "showtitle", "season", "episode"],
            "limits": {"start": 0, "end": limit}, "sort": {"method": "title", "order": "ascending"}})
        entries = result.get(plural.lower(), [])
        return {"matches": entries, "ambiguous": len(entries) > 1, "selection": "Выбери точный ID; первый результат автоматически не запускается"}

    async def details(self, media, media_id):
        if media not in {"movie", "episode", "tvshow"} or type(media_id) is not int or media_id < 0:
            raise ValueError("🛑 Нужен точный ID медиа")
        prefix = {"movie": "Movie", "episode": "Episode", "tvshow": "TVShow"}[media]
        props = ["title", "plot", "year", "genre", "rating"]
        if media != "tvshow":
            props += ["file", "resume", "streamdetails", "runtime"]
        return safe(await self.rpc.call("VideoLibrary.Get" + prefix + "Details", {media + "id": media_id, "properties": props}))

    async def state(self, player_id=None):
        players = await self.rpc.call("Player.GetActivePlayers")
        result = []
        for player in players:
            pid = player["playerid"]
            if player_id is not None and pid != player_id:
                continue
            properties = await self.rpc.call("Player.GetProperties", {"playerid": pid, "properties": PROPERTIES})
            current = await self.rpc.call("Player.GetItem", {"playerid": pid, "properties": ["title", "file", "showtitle", "season", "episode", "duration"]})
            result.append({"player_id": pid, "item": current.get("item", {}), **properties,
                           "time_seconds": seconds(properties.get("time", {})), "total_seconds": seconds(properties.get("totaltime", {}))})
        gui = await self.rpc.call("GUI.GetProperties", {"properties": ["currentwindow", "currentcontrol", "fullscreen"]})
        labels = await self.rpc.call("XBMC.GetInfoLabels", {"labels": ["VideoPlayer.VideoCodec", "VideoPlayer.VideoResolution", "VideoPlayer.AudioCodec", "Player.Process(videodecoder)", "Player.Process(videofps)"]})
        return safe({"players": result, "gui": gui, "video": labels, "event_cursor": self.rpc.event_sequence})

    async def active(self, player_id=None):
        players = await self.rpc.call("Player.GetActivePlayers")
        selected = [p for p in players if player_id is None or p["playerid"] == player_id]
        if len(selected) != 1:
            raise ValueError("🛑 Нет единственного выбранного плеера; получи kodi_state и укажи player_id")
        return selected[0]["playerid"]

    async def open(self, kind, value, resume=False, wait_seconds=20):
        target = item(kind, value)
        if type(resume) not in {bool, int, float} or (not isinstance(resume, bool) and (not math.isfinite(resume) or not 0 <= resume <= 100)):
            raise ValueError("🛑 Resume: false с начала, true продолжить либо 0..100 процентов")
        if not 2 <= wait_seconds <= 45:
            raise ValueError("🛑 Ожидание запуска 2..45 секунд")
        async with self.mutation:
            await self.rpc.connect()
            cursor = self.rpc.event_sequence
            await self.rpc.call("Player.Open", {"item": target, "options": {"resume": resume}})
            deadline, av, initial = time.monotonic() + wait_seconds, None, None
            current = {}
            while time.monotonic() < deadline:
                events = [e for e in self.rpc.events if e["sequence"] > cursor and e["method"] == "Player.OnAVStart"]
                if events:
                    av = events[-1]
                current = await self.state()
                playing = [p for p in current["players"] if p.get("speed", 0) > 0]
                if playing and av:
                    candidate = playing[0]
                    if kind in {"movie", "episode"} and (candidate["item"].get("id") != value or candidate["item"].get("type") != kind):
                        raise KodiError("🛑 Стартовал другой объект библиотеки")
                    if initial and candidate["player_id"] == initial["player_id"] and candidate["time_seconds"] > initial["time_seconds"] + 0.2:
                        return {"acknowledged": True, "av_started": True, "progressing": True,
                                "physical_screen_observed": False, "state": current}
                    initial = candidate
                await asyncio.sleep(0.75)
            return {"acknowledged": True, "av_started": bool(av), "progressing": False,
                    "physical_screen_observed": False, "message": "⚠️ Запрос принят, воспроизведение не подтверждено; проверь диалог/авторизацию дополнения", "state": current}

    async def play_pause(self, play, player_id=None):
        async with self.mutation:
            pid = await self.active(player_id)
            await self.rpc.call("Player.PlayPause", {"playerid": pid, "play": play})
            state = await self.state(pid)
            actual = state["players"][0]["speed"] if state["players"] else None
            return {"verified": actual is not None and (actual > 0 if play else actual == 0), "state": state}

    async def stop(self, player_id=None):
        async with self.mutation:
            pid = await self.active(player_id)
            await self.rpc.call("Player.Stop", {"playerid": pid})
            return await self.state()

    async def seek(self, mode, value_seconds, player_id=None):
        if mode not in {"relative", "absolute"} or not math.isfinite(value_seconds):
            raise ValueError("🛑 Укажи relative/absolute и конечное число секунд")
        async with self.mutation:
            pid = await self.active(player_id)
            current = (await self.state(pid))["players"][0]
            if not current["canseek"]:
                raise KodiError("🛑 Текущий поток не поддерживает перемотку")
            target = value_seconds + (current["time_seconds"] if mode == "relative" else 0)
            if target < 0 or (current["total_seconds"] and target > current["total_seconds"]):
                raise ValueError("🛑 Позиция вне длительности видео")
            whole, ms = divmod(round(target * 1000), 1000)
            hours, rem = divmod(whole, 3600)
            minutes, sec = divmod(rem, 60)
            await self.rpc.call("Player.Seek", {"playerid": pid, "value": {"time": {"hours": hours, "minutes": minutes, "seconds": sec, "milliseconds": ms}}})
            return {"requested_seconds": target, "state": await self.state(pid)}

    async def queue(self, action, playlist_id=1, kind=None, value=None, position=None, other_position=None):
        if playlist_id not in {0, 1} or action not in {"get", "add", "insert", "remove", "swap", "clear", "goto"}:
            raise ValueError("🛑 Проверь playlist_id 0/1 и действие очереди")
        async with self.mutation:
            current = await self.rpc.call("Playlist.GetItems", {"playlistid": playlist_id, "properties": ["title", "file", "duration"]})
            size = current.get("limits", {}).get("total", len(current.get("items", [])))
            if action == "get":
                return safe(current)
            params = {"playlistid": playlist_id}
            if action in {"insert", "remove", "swap", "goto"}:
                maximum = size if action == "insert" else size - 1
                if type(position) is not int or not 0 <= position <= maximum:
                    raise ValueError("🛑 Позиция вне текущей очереди")
                params["position"] = position
            if action in {"add", "insert"}:
                params["item"] = item(kind, value)
            if action == "swap":
                if type(other_position) is not int or not 0 <= other_position < size:
                    raise ValueError("🛑 Вторая позиция вне очереди")
                params.pop("position")
                params.update(position1=position, position2=other_position)
            if action == "goto":
                pid = await self.active()
                actual = await self.rpc.call("Player.GetProperties", {"playerid": pid, "properties": ["playlistid"]})
                if actual.get("playlistid") != playlist_id:
                    raise ValueError("🛑 Активный плеер использует другую очередь")
                await self.rpc.call("Player.GoTo", {"playerid": pid, "to": position})
            else:
                await self.rpc.call("Playlist." + action.capitalize(), params)
            return safe(await self.rpc.call("Playlist.GetItems", {"playlistid": playlist_id, "properties": ["title", "file", "duration"]}))

    async def tracks(self, player_id=None, audio_index=None, subtitle_index=None, subtitles_enabled=None):
        async with self.mutation:
            pid = await self.active(player_id)
            current = (await self.state(pid))["players"][0]
            for index, tracks in [(audio_index, current.get("audiostreams", [])), (subtitle_index, current.get("subtitles", []))]:
                if index is not None and (type(index) is not int or index not in {s["index"] for s in tracks}):
                    raise ValueError("🛑 Выбери индекс из реально доступных дорожек")
            if audio_index is not None:
                await self.rpc.call("Player.SetAudioStream", {"playerid": pid, "stream": audio_index})
            if subtitle_index is not None or subtitles_enabled is not None:
                if subtitles_enabled is not None and type(subtitles_enabled) is not bool:
                    raise ValueError("🛑 subtitles_enabled должен быть true/false")
                params = {"playerid": pid, "subtitle": subtitle_index if subtitle_index is not None else ("on" if subtitles_enabled else "off")}
                if subtitles_enabled is not None:
                    params["enable"] = subtitles_enabled
                await self.rpc.call("Player.SetSubtitle", params)
            return await self.state(pid)

    async def input(self, action):
        allowed = {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "select": "Select", "back": "Back",
                   "home": "Home", "info": "Info", "contextmenu": "ContextMenu"}
        if action not in allowed:
            raise ValueError("🛑 Действие ввода не разрешено")
        await self.rpc.call("Input." + allowed[action])
        await asyncio.sleep(0.2)
        return await self.state()

    async def browse(self, url, limit=30):
        item("file", url)
        if urlsplit(url).scheme != "plugin" or not 1 <= limit <= 100:
            raise ValueError("🛑 Browse разрешён для дополнения YouTube, лимит 1..100")
        return safe(await self.rpc.call("Files.GetDirectory", {"directory": url, "media": "video", "properties": ["title", "duration"], "limits": {"start": 0, "end": limit}}))

    async def youtube_play(self, video_id, wait_seconds=20):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("🛑 Нужен 11-символьный YouTube video_id")
        return await self.open("file", "plugin://plugin.video.youtube/play/?" + urlencode({"video_id": video_id}), False, wait_seconds)

    async def gui(self, window, path=None):
        if window not in {"home", "videos", "addonbrowser"}:
            raise ValueError("🛑 Окно Kodi не разрешено")
        params = {"window": window}
        if path is not None:
            if path not in {"addons://repository.xbmc.org/", "addons://repository.xbmc.org/xbmc.addon.video", "addons://sources/video/"}:
                item("file", path)
                if not path.startswith("plugin://plugin.video.youtube/"):
                    raise ValueError("🛑 Разрешено окно официального репозитория или YouTube")
            params["parameters"] = [path]
        await self.rpc.call("GUI.ActivateWindow", params)
        await asyncio.sleep(0.3)
        return await self.state()
