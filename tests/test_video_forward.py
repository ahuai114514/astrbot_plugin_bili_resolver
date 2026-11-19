"""Isolated tests; no AstrBot framework initialization or chat sends."""

import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


class Component:
    def __init__(self, text=None, **kwargs):
        self.text = text
        self.__dict__.update(kwargs)

    @classmethod
    def fromURL(cls, url):
        return cls(file=url)

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=Path(path).as_uri())


class Star:
    def __init__(self, context):
        pass


def load_plugin():
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.api.message_components",
    ):
        sys.modules[name] = types.ModuleType(name)
    api = sys.modules["astrbot.api"]
    api.logger = logging.getLogger("test")
    event = sys.modules["astrbot.api.event"]
    event.filter = types.SimpleNamespace(
        command=lambda *a: lambda f: f,
        event_message_type=lambda *a: lambda f: f,
        EventMessageType=types.SimpleNamespace(ALL=1),
    )
    event.AstrMessageEvent = object
    star = sys.modules["astrbot.api.star"]
    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace()
    star.register = lambda *a: lambda cls: cls
    components = sys.modules["astrbot.api.message_components"]
    for name in ("Plain", "Image", "Video", "Node", "Nodes"):
        setattr(components, name, type(name, (Component,), {}))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    return importlib.import_module("astrbot_plugin_bili_resolver.main")


main = load_plugin()
video = importlib.import_module("astrbot_plugin_bili_resolver.video_download")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("BV1LiVKzFEVf", ({"bvid": "BV1LiVKzFEVf"}, 1)),
        ("https://www.bilibili.com/video/av123?p=2", ({"aid": "123"}, 2)),
        ("https://live.bilibili.com/123", None),
        ("https://www.bilibili.com/opus/123", None),
    ],
)
def test_request(text, expected):
    url, page, _ = main.analysis_bilibili.extract(text)
    assert video.video_request(url, page) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.bilivideo.com/a.mp4", True),
        ("https://a.akamaized.net/a.mp4", True),
        ("http://127.0.0.1/a", False),
        ("https://bilivideo.com.evil.test/a", False),
        ("file:///etc/passwd", False),
    ],
)
def test_media_hosts(url, expected):
    assert video.allowed_media_url(url) == expected


class Event:
    def __init__(self, text="BV1LiVKzFEVf"):
        self.message_str = text
        self.message_obj = types.SimpleNamespace(
            group_id="123", raw_message=None, message=[]
        )
        self.send = AsyncMock()
        self.get_platform_name = lambda: "aiocqhttp"
        self.get_self_id = lambda: "123456"
        self.get_extra = lambda key, default=None: getattr(self, "extras", {}).get(
            key, default
        )
        self.chain_result = lambda chain: chain
        self.plain_result = lambda text: text
        self.stop_event = lambda: None


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        main.StarTools, "get_data_dir", lambda name: tmp_path, raising=False
    )
    plugin = main.BilibiliAnalysis(None, {"video_send_path": "/host/videos"})
    plugin._get_session = AsyncMock(return_value=object())

    async def download(session, request, directory, max_bytes):
        path = directory / "video.mp4"
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(main, "download_video", download)
    return plugin


def test_forward_retains_chain_and_cleans(plugin, tmp_path):
    event = Event()
    chain = main._format_msg(["title -UP BV1LiVKzFEVf ", ["\nhttps://original "]])
    assert asyncio.run(plugin._send_video_forward(event, event.message_str, chain))
    nodes = event.send.call_args.args[0][0].nodes
    assert len(nodes) == 2
    assert nodes[0].content is chain
    assert chain[0].text == "title -UP BV1LiVKzFEVf \nhttps://original "
    assert nodes[0].uin == "123456"
    assert nodes[1].content[0].file.startswith("file:///host/videos/bili-")
    assert list((tmp_path / "videos").iterdir()) == []


def test_oversized_video_keeps_parse_and_adds_notice(plugin, monkeypatch, tmp_path):
    event = Event()
    chain = main._format_msg(["original parse"])
    monkeypatch.setattr(
        main,
        "download_video",
        AsyncMock(side_effect=video.VideoTooLargeError("Video exceeds size limit")),
    )

    assert asyncio.run(plugin._send_video_forward(event, event.message_str, chain))
    nodes = event.send.call_args.args[0][0].nodes
    assert len(nodes) == 2
    assert nodes[0].content is chain
    assert nodes[1].content[0].text == "视频过大，未下载。"
    assert list((tmp_path / "videos").iterdir()) == []


@pytest.mark.parametrize("stage", ["download", "send", "cancel"])
def test_failure_and_cancel_cleanup(plugin, tmp_path, monkeypatch, stage):
    event = Event()
    if stage == "send":
        event.send.side_effect = RuntimeError("send failed")
    else:
        monkeypatch.setattr(
            main,
            "download_video",
            AsyncMock(
                side_effect=(
                    asyncio.CancelledError()
                    if stage == "cancel"
                    else RuntimeError("download failed")
                )
            ),
        )
    if stage == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(plugin._send_video_forward(event, event.message_str, []))
    else:
        assert not asyncio.run(plugin._send_video_forward(event, event.message_str, []))
    assert list((tmp_path / "videos").iterdir()) == []


@pytest.mark.parametrize("search", [False, True])
def test_both_handlers_keep_fallback(plugin, monkeypatch, search):
    monkeypatch.setattr(
        main, "bili_keyword", AsyncMock(return_value=["original result"])
    )
    monkeypatch.setattr(
        main, "search_bili_by_title", AsyncMock(return_value="BV1LiVKzFEVf")
    )
    plugin._send_video_forward = AsyncMock(return_value=False)
    event = Event("/搜视频 青衣" if search else "BV1LiVKzFEVf")

    async def run():
        handler = plugin.search_video if search else plugin.on_message
        return [result async for result in handler(event)]

    results = asyncio.run(run())
    assert results[0][0].text == "original result"
    assert plugin._send_video_forward.call_args.args[1] == "BV1LiVKzFEVf"


def test_auto_parse_skips_messages_matched_by_commands(plugin, monkeypatch):
    monkeypatch.setattr(main, "bili_keyword", AsyncMock())
    event = Event("/点歌 BV1LiVKzFEVf")
    event.extras = {"handlers_parsed_params": {"singing_plugin.song": {}}}

    async def run():
        return [result async for result in plugin.on_message(event)]

    assert asyncio.run(run()) == []
    main.bili_keyword.assert_not_awaited()


def test_selected_part_and_backup(tmp_path, monkeypatch):
    api = AsyncMock(
        side_effect=[
            {"pages": [{"cid": 1}, {"cid": 2}]},
            {
                "durl": [
                    {
                        "url": "https://a.bilivideo.com/first",
                        "backup_url": ["https://a.bilivideo.com/backup"],
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(video, "_api", api)
    calls = []

    async def download(session, url, path, max_bytes):
        calls.append(url)
        if len(calls) == 1:
            raise video.VideoDownloadError("unavailable")
        path.write_bytes(b"source")

    async def remux(source, target):
        target.write_bytes(b"mp4")

    monkeypatch.setattr(video, "_download", download)
    monkeypatch.setattr(video, "_remux", remux)
    path = asyncio.run(
        video.download_video(object(), ({"aid": "123"}, 2), tmp_path, 100)
    )
    assert path.read_bytes() == b"mp4"
    assert api.call_args_list[1].args[2]["cid"] == 2
    assert len(calls) == 2
    assert not (tmp_path / "source.bin").exists()


class Response:
    status = 200
    content_length = None

    def __init__(self, chunks):
        self.headers = {}
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


def test_stream_size_limit(tmp_path):
    session = types.SimpleNamespace(get=lambda *a, **k: Response([b"123", b"456"]))
    with pytest.raises(video.VideoDownloadError, match="size limit"):
        asyncio.run(
            video._download(
                session, "https://a.bilivideo.com/a", tmp_path / "download", 5
            )
        )


def test_redirect_revalidated(tmp_path):
    response = Response([])
    response.status = 302
    response.headers = {"Location": "http://127.0.0.1/private"}
    session = types.SimpleNamespace(get=lambda *a, **k: response)
    with pytest.raises(video.VideoDownloadError, match="Untrusted"):
        asyncio.run(
            video._download(
                session, "https://a.bilivideo.com/a", tmp_path / "download", 100
            )
        )


def test_partial_range_rejected(tmp_path):
    response = Response([b"123"])
    response.status = 206
    response.headers = {"Content-Range": "bytes 0-2/100"}
    session = types.SimpleNamespace(get=lambda *a, **k: response)
    with pytest.raises(video.VideoDownloadError, match="Incomplete media range"):
        asyncio.run(
            video._download(
                session, "https://a.bilivideo.com/a", tmp_path / "download", 100
            )
        )
