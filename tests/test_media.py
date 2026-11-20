"""
Tests for media inspection (ffprobe output parsing) and retriever file
verification.
"""
from types import SimpleNamespace

import pytest

from stagehand import media
from stagehand.retrievers import base as retrievers_base
from stagehand.retrievers.base import RetrieverBase, RetrieverError

FFPROBE_OUTPUT = {
    'streams': [
        {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080},
        {'codec_type': 'video', 'codec_name': 'mjpeg', 'width': 600, 'height': 600,
         'disposition': {'attached_pic': 1}},
        {'codec_type': 'audio', 'codec_name': 'aac', 'tags': {'language': 'ENG'}},
        {'codec_type': 'audio', 'codec_name': 'ac3'},
        {'codec_type': 'subtitle', 'codec_name': 'subrip'},
    ],
    'format': {'duration': '1320.5'},
}


def test_parse_ffprobe_output():
    info = media.parse_ffprobe_output(FFPROBE_OUTPUT)
    assert info.duration == 1320.5
    assert info.video == [media.VideoTrack(1920, 1080, 'h264')]
    assert info.audio == [media.AudioTrack('eng', 'aac'), media.AudioTrack(None, 'ac3')]


def test_parse_ffprobe_output_partial():
    info = media.parse_ffprobe_output({'streams': [{'codec_type': 'video'}], 'format': {}})
    assert info.duration is None
    assert info.video == [media.VideoTrack(0, 0, None)]


@pytest.mark.parametrize('lang, expected', [
    ('en', {'en', 'eng'}),
    ('de', {'de', 'ger', 'deu'}),
    ('FR', {'fr', 'fre', 'fra'}),
    ('deu', {'de', 'ger', 'deu'}),
    ('xx', {'xx'}),
])
def test_language_codes(lang, expected):
    assert media.language_codes(lang) <= expected | {lang.lower()}
    assert lang.lower() in media.language_codes(lang)
    assert expected <= media.language_codes(lang)


async def test_probe_without_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr(media, 'ffprobe_path', lambda: None)
    assert await media.probe(str(tmp_path / 'x')) is None


@pytest.mark.skipif(not media.ffprobe_path(), reason='ffprobe not installed')
async def test_probe_unidentifiable_file(tmp_path):
    f = tmp_path / 'junk.mkv'
    f.write_bytes(b'\0' * 4096)
    assert await media.probe(str(f)) is None


class Verifier:
    """Runs RetrieverBase.verify_result_file against canned MediaInfo."""
    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.file = tmp_path / 'ep.mkv'
        self.file.write_bytes(b'x' * 100)
        monkeypatch.setattr(media, 'ffprobe_path', lambda: '/usr/bin/ffprobe')

    async def __call__(self, info, quality='HD', filename='Show.s01e01.mkv', language='en'):
        async def probe(path):
            return info
        self.monkeypatch.setattr(media, 'probe', probe)
        episode = SimpleNamespace(series=SimpleNamespace(cfg=SimpleNamespace(language=language)))
        result = SimpleNamespace(quality=quality, filename=filename)
        return await RetrieverBase().verify_result_file(episode, result, str(self.file))


@pytest.fixture
def verify(monkeypatch, tmp_path):
    return Verifier(monkeypatch, tmp_path)


def info(height=1080, duration=1300.0, langs=('eng',)):
    return media.MediaInfo(duration, [media.VideoTrack(height * 16 // 9, height)],
                           [media.AudioTrack(lang) for lang in langs])


async def test_verify_ok(verify):
    assert await verify(info()) is True
    assert await verify(info(height=480), quality='SD') is True
    assert await verify(info(height=2160), quality='UHD') is True
    assert await verify(info(langs=('ger',)), language='de') is True


async def test_verify_wrong_quality(verify):
    with pytest.raises(RetrieverError, match='resolution'):
        await verify(info(height=480), quality='HD')
    with pytest.raises(RetrieverError, match='resolution'):
        await verify(info(height=1080), quality='SD')


async def test_verify_too_short(verify):
    with pytest.raises(RetrieverError, match='duration'):
        await verify(info(duration=60))
    # Unknown duration (e.g. partial file) is not disqualifying.
    assert await verify(info(duration=None)) is True


async def test_verify_language(verify):
    with pytest.raises(RetrieverError, match='audio track'):
        await verify(info(langs=('fre',)))
    # 'und' is assumed to be English when English is wanted.
    assert await verify(info(langs=('fre', 'und'))) is True
    with pytest.raises(RetrieverError, match='dubbed'):
        await verify(info(langs=()), filename='Show.s01e01.DUBBED.mkv')


async def test_verify_no_video(verify):
    with pytest.raises(RetrieverError, match='no video'):
        await verify(media.MediaInfo(1300.0, [], [media.AudioTrack('eng')]))


async def test_verify_unidentified(verify, monkeypatch):
    assert await verify(None) is False
    monkeypatch.setattr(retrievers_base, 'MAX_UNIDENTIFIED_BYTES', 10)
    with pytest.raises(RetrieverError, match='identify'):
        await verify(None)


async def test_verify_without_ffprobe(verify, monkeypatch):
    monkeypatch.setattr(media, 'ffprobe_path', lambda: None)
    assert await verify(info()) is None
