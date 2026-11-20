from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, ClassVar

from .. import media
from ..config import config

if TYPE_CHECKING:
    from ..searchers.base import SearchResult
    from ..toolbox import FutureProgress
    from ..tvdb import Episode

log = logging.getLogger('stagehand.retrievers')


class RetrieverError(Exception):
    pass


class RetrieverSoftError(RetrieverError):
    """
    Generic retriever error indicating failure fetching a specific result
    (e.g. file not found).  Other search results for this retriever could
    be tried.
    """


class RetrieverHardError(RetrieverError):
    """
    Generic retriever error indicating a error not with the specific search
    result but with the retriever itself or its configuration (e.g. invalid
    credentials). No other search results for this retriever should be tried.
    """


class RetrieverAborted(RetrieverError):
    pass


class RetrieverAbortedSoft(RetrieverAborted, RetrieverSoftError):
    """
    Raised when the retrieval of the active search result was aborted.  If
    there are other search results for the episode being downloaded, they
    will be tried.
    """


class RetrieverAbortedHard(RetrieverAborted, RetrieverHardError):
    """
    Like RetrieverAbortedSoft, except no further results will be tried for the
    episode.
    """


# Minimum video height (inclusive) and maximum height (inclusive, or None for
# no maximum) for each quality level.
QUALITY_HEIGHTS: dict[str, tuple[int, int | None]] = {
    'SD': (240, 540),
    'HD': (600, None),
    'UHD': (1600, None),
}

# Files shorter than this many seconds are rejected.
MIN_DURATION = 300

# If the file can't be identified after this many bytes, give up.
MAX_UNIDENTIFIED_BYTES = 10 * 1024 * 1024


class RetrieverBase:
    # Values must be populated by subclasses.
    # The internal name of the plugin (lowercase, no spaces).
    NAME: ClassVar[str]
    # The human-readable name for the plugin.
    PRINTABLE_NAME: ClassVar[str]
    # A list of supported types the retriever plugins supports (e.g. http, nzb,
    # torrent).
    SUPPORTED_TYPES: ClassVar[tuple[str, ...]] = ()
    # False if the user may disable the plugin, or True if it is always active.
    ALWAYS_ENABLED: ClassVar[bool] = False

    async def _retrieve(self, progress: FutureProgress, episode: Episode, result: SearchResult,
                        outfile: str) -> None:
        """
        Retrieve the given SearchResult object.  Subclasses must implement.
        """
        raise NotImplementedError

    async def retrieve(self, progress: FutureProgress, episode: Episode, result: SearchResult,
                       outfile: str) -> None:
        await self._retrieve(progress, episode, result, outfile)

    async def verify_result_file(self, episode: Episode, result: SearchResult, outfile: str) -> bool | None:
        """
        Ensure the given (possibly partial) video file matches the
        user-requested criteria with respect to resolution, duration, and
        audio language.

        :returns: True if result is verified, False if the file could not be
                  identified (yet), or None if verification isn't possible
                  because ffprobe isn't installed.
        :raises: RetrieverError if the video file was valid but did not pass
                 verification, which will cause the transfer to abort
        """
        if not media.ffprobe_path():
            log.warning('ffprobe not available; skipping file verification')
            return None

        info = await media.probe(outfile)
        if not info:
            if os.path.getsize(outfile) > MAX_UNIDENTIFIED_BYTES:
                # We have 10MB of the file and still can't parse it.  Consider
                # it corrupt or unknown.
                raise RetrieverError('could not identify file after 10MB')
            return False
        if not info.video:
            raise RetrieverError('file has no video tracks')

        # Find the video track with the highest (y) resolution.
        video = max(info.video, key=lambda track: track.height)
        if result.quality in QUALITY_HEIGHTS:
            lo, hi = QUALITY_HEIGHTS[result.quality]
            if video.height < lo or (hi is not None and video.height > hi):
                raise RetrieverError(f'video resolution {video.width}x{video.height} does not satisfy '
                                     f'requested {result.quality} quality')

        if info.duration is not None and info.duration < MIN_DURATION:
            raise RetrieverError(f'file duration {info.duration} is not long enough')

        # See if the audio is the right language.  If the language is specified in any of
        # the tracks, make sure one of them is the user-preferred language.
        langs = {track.language or 'und' for track in info.audio}
        if langs <= {'und'}:
            # No languages specified in any track. Disqualify if 'dubbed' appears in
            # the result filename.
            if re.search(r'\bdubbed\b', result.filename, re.I):
                raise RetrieverError('filename indicates dubbed audio but no language codes specified')
        else:
            lang = str(episode.series.cfg.language or config.misc.language)
            wanted = media.language_codes(lang)
            log.info('looking for preferred audio track language %s', lang)
            if not wanted & langs:
                # There are audio tracks with defined languages, but none of
                # them are the language we want.  Heuristic (based on real
                # world content): if the desired language is English and one
                # of the tracks is undefined, assume it's English.
                if 'eng' in wanted and 'und' in langs:
                    log.warning("no English track found, but assuming 'und' track is English")
                else:
                    raise RetrieverError(f'no {lang} audio track in file')

        log.debug('%s looks good (%dx%d %s, audio languages: %s)', result.filename, video.width, video.height,
                  video.codec, ', '.join(sorted(langs)))
        return True
