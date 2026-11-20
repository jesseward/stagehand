"""
Inspection of video files using ``ffprobe`` (part of FFmpeg).

This replaces the bundled kaa.metadata library.  ffprobe is optional: if it
isn't installed, :func:`probe` returns None and callers should skip any
checks that depend on it.
"""
from __future__ import annotations

__all__ = ['AudioTrack', 'MediaInfo', 'VideoTrack', 'ffprobe_path', 'language_codes', 'probe']

import asyncio
import functools
import json
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger('stagehand.media')

PROBE_TIMEOUT = 30


@dataclass(frozen=True, slots=True)
class VideoTrack:
    width: int
    height: int
    codec: str | None = None


@dataclass(frozen=True, slots=True)
class AudioTrack:
    # ISO 639-2 language code as stored in the container, or None if unspecified.
    language: str | None = None
    codec: str | None = None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    # Duration in seconds, if known.
    duration: float | None = None
    video: list[VideoTrack] = field(default_factory=list)
    audio: list[AudioTrack] = field(default_factory=list)


@functools.cache
def ffprobe_path() -> str | None:
    """
    Returns the path to the ffprobe executable, or None if it isn't installed.
    """
    return shutil.which('ffprobe')


def parse_ffprobe_output(data: dict[str, Any]) -> MediaInfo:
    """
    Converts the JSON output of ``ffprobe -show_format -show_streams`` into a
    :class:`MediaInfo`.
    """
    video, audio = [], []
    for stream in data.get('streams', []):
        kind = stream.get('codec_type')
        if kind == 'video':
            if stream.get('disposition', {}).get('attached_pic'):
                # Cover art, not a real video track.
                continue
            video.append(VideoTrack(int(stream.get('width') or 0), int(stream.get('height') or 0),
                                    stream.get('codec_name')))
        elif kind == 'audio':
            lang = (stream.get('tags') or {}).get('language')
            audio.append(AudioTrack(lang.lower() if lang else None, stream.get('codec_name')))

    duration = None
    try:
        duration = float(data['format']['duration'])
    except (KeyError, TypeError, ValueError):
        pass
    return MediaInfo(duration, video, audio)


async def probe(path: str) -> MediaInfo | None:
    """
    Inspects the given (possibly partially downloaded) file.

    :returns: a MediaInfo object, or None if ffprobe is unavailable or could
              not identify the file
    """
    ffprobe = ffprobe_path()
    if not ffprobe:
        return None

    proc = await asyncio.create_subprocess_exec(
        ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning('ffprobe timed out inspecting %s', path)
        return None

    if proc.returncode != 0:
        log.debug('ffprobe could not identify %s: %s', path, stderr.decode(errors='replace').strip())
        return None
    try:
        info = parse_ffprobe_output(json.loads(stdout))
    except ValueError:
        log.warning('unexpected output from ffprobe for %s', path)
        return None
    return info if (info.video or info.audio) else None


# ISO 639-1 (two letter) codes mapped to ISO 639-2 (three letter) codes.  Where
# the bibliographic (B) and terminologic (T) codes differ, both are listed
# since either may appear in media files.
_ISO639 = {
    'ar': ('ara',), 'bg': ('bul',), 'bn': ('ben',), 'ca': ('cat',), 'cs': ('cze', 'ces'), 'cy': ('wel', 'cym'),
    'da': ('dan',), 'de': ('ger', 'deu'), 'el': ('gre', 'ell'), 'en': ('eng',), 'es': ('spa',), 'et': ('est',),
    'eu': ('baq', 'eus'), 'fa': ('per', 'fas'), 'fi': ('fin',), 'fr': ('fre', 'fra'), 'ga': ('gle',),
    'he': ('heb',), 'hi': ('hin',), 'hr': ('hrv',), 'hu': ('hun',), 'id': ('ind',), 'is': ('ice', 'isl'),
    'it': ('ita',), 'ja': ('jpn',), 'ko': ('kor',), 'lt': ('lit',), 'lv': ('lav',), 'ms': ('may', 'msa'),
    'nl': ('dut', 'nld'), 'no': ('nor',), 'nb': ('nob', 'nor'), 'nn': ('nno', 'nor'), 'pl': ('pol',),
    'pt': ('por',), 'ro': ('rum', 'ron'), 'ru': ('rus',), 'sk': ('slo', 'slk'), 'sl': ('slv',), 'sr': ('srp',),
    'sv': ('swe',), 'ta': ('tam',), 'te': ('tel',), 'th': ('tha',), 'tr': ('tur',), 'uk': ('ukr',),
    'ur': ('urd',), 'vi': ('vie',), 'zh': ('chi', 'zho'),
}


def language_codes(lang: str) -> set[str]:
    """
    Returns the set of codes that may identify the given language in a media
    file.

    :param lang: a two letter ISO 639-1 code, or three letter ISO 639-2 code
    """
    lang = lang.lower()
    if len(lang) == 2:
        return {lang, *_ISO639.get(lang, ())}
    for two, codes in _ISO639.items():
        if lang in codes:
            return {two, *codes}
    return {lang}
