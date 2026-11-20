"""
Miscellaneous helpers shared across Stagehand.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import re
from collections.abc import Mapping
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .toolbox.config import get_description

if TYPE_CHECKING:
    from .tvdb import Episode


def load_plugins(type: str, names: list[str]) -> tuple[dict[str, ModuleType], dict[str, Exception]]:
    """
    Imports the named plugin modules of the given type (e.g. ``searchers``).

    :returns: a 2-tuple of dicts (valid, invalid), where valid maps plugin
              names to modules, and invalid maps names to the exception
              raised while importing.
    """
    from .config import config

    valid: dict[str, ModuleType] = {}
    invalid: dict[str, Exception] = {}
    for name in names:
        try:
            module = importlib.import_module('.' + name, 'stagehand.' + type)
        except Exception as e:
            invalid[name] = e
            continue

        if hasattr(module, 'modconfig'):
            getattr(config, type).add_variable(name, module.modconfig)
        valid[name] = module
        if hasattr(module, 'load'):
            module.load()
    return valid, invalid


async def invoke_plugins(plugins: Mapping[str, ModuleType], func: str, *args: Any) -> list[Any]:
    """
    Invokes an async function in parallel on all supplied plugins.

    :param plugins: a dict of plugins (name -> plugin)
    :param func: the name of the function to invoke if it exists
    :param args: the arguments to pass to the plugin function
    :returns: list of results
    """
    return await asyncio.gather(*(getattr(p, func)(*args) for p in plugins.values() if hasattr(p, func)))


def fixsep(s: str, path: bool = True) -> str:
    """
    Applies the configured separator policy to the given string.

    :param path: if True, return a result that can be used as a path name on the
                 filesystem (replace path separators and colons)
    """
    from .config import config
    if path:
        # TODO: fix / : " ? * < > |
        s = s.replace(os.path.sep, ' ')
        # Colons are problematic on CIFS mounts.  Even on filesystems where
        # they're allowed, they'll cause problems if the user wants to copy a
        # file to a filesystem where they're not.  e.g. Linux with an ntfs-3g
        # mount will happily let you copy a file with a colon in its name, and
        # then the directory won't be readable by an actual Windows machine.
        # So, just replace them with spaces.
        s = s.replace(':', ' ')
        # For the same reason, strip question marks.
        s = s.replace('?', '')
        # Now condense multiple spaces.
        s = re.sub(r'\s+', ' ', s)
    s = s.replace(' ', config.naming.separator)
    return s


def fixquotes(u: str | None) -> str | None:
    """
    Given a unicode string, replaces "smart" quotes, ellipses, etc.
    with ASCII equivalents.
    """
    if not u:
        return u
    # Double quotes
    u = u.replace('\u201c', '"').replace('\u201d', '"')
    # Single quotes
    u = u.replace('\u2018', "'").replace('\u2019', "'")
    # Endash
    u = u.replace('\u2014', '--')
    # Ellipses
    u = u.replace('\u2026', '...')
    return u


def remove_stop_words(s: str) -> str:
    """
    Removes (English) stop words from the given string.
    """
    stopwords = 'a', 'the'
    words = [word for word in s.split() if word.lower() not in stopwords]
    # Join remaining words and remove whitespace.
    return ' '.join(words)


def cfgdesc2html(item: Any) -> str:
    """
    Given a config item, returns the description with paragraph breaks
    (double newline) converted to <br> for use in HTML.
    """
    desc = get_description(item)
    return re.sub(r'\n\s*\n', '<br/><br/>', desc)


def name_to_url_segment(name: str) -> str:
    """
    Given some kind of name, return a lower case string without any punctuation
    or spaces, sutiable for use as a URL path segment.
    """
    name = name.lower().replace('&', 'and').replace(' ', '_')
    if name.startswith('the_'):
        name = name[4:]
    return re.sub(r'\W', '', name)


def episode_status_icon_info(ep: Episode) -> tuple[str, str]:
    """
    Given an Episode object, returns the icon details for the episode's status.

    :params ep: Episode object
    :returns: (status, title)

    status is one of 'forced', 'have', 'ignore', 'need', or 'future' and title
    is a printable string for the state.
    """
    if ep.status == ep.STATUS_NEED_FORCED:
        return 'need-forced', 'Needed (Forced by User)'
    elif ep.status == ep.STATUS_HAVE:
        return 'have', 'Downloaded'
    elif ep.status == ep.STATUS_IGNORE or ep.obsolete or ep.season.number == 0:
        return 'ignore', 'Ignored'
    elif ep.aired:
        return 'need', 'Needed'
    else:
        return 'future', 'Not Aired'
