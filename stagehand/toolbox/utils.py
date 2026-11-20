# -----------------------------------------------------------------------------
# utils.py - Miscellaneous system utilities
# -----------------------------------------------------------------------------
# Copyright 2010-2014 Dirk Meyer, Jason Tackaberry
#
# Originally from kaa.base, (partially) ported to Python 3 by Jason Tackaberry
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
# -----------------------------------------------------------------------------
from __future__ import annotations

__all__ = ['get_encoding', 'tobytes', 'tostr', 'fsname', 'get_temp_path', 'tempfile', 'daemonize']

import functools
import getpass
import locale
import os
import stat
import sys
import tempfile as _tempfile
from typing import Any


def _detect_encoding() -> str | None:
    """
    Find correct encoding from default locale.
    """
    # Some locales report encodings like 'utf_8_valencia' which Python doesn't
    # know.  We try stripping off the right-most parts until we find an
    # encoding that works.
    e: str | None = locale.getpreferredencoding(False)
    while e:
        try:
            ''.encode(e)
            return e
        except LookupError:
            pass
        if '_' not in e:
            break
        e = e.rsplit('_', 1)[0]
    return None


# If encoding can't be detected from locale, default to UTF-8.
ENCODING = _detect_encoding() or 'utf-8'


def get_encoding() -> str:
    """
    Return the current encoding.
    """
    return ENCODING


def tobytes(value: Any, encoding: str | None = None, desperate: bool = True, coerce: bool = False,
            fs: bool = False) -> Any:
    """
    Convert (if necessary) the given value to a "string of bytes", agnostic to
    any character encoding.

    :param value: the value to be converted to a string of bytes
    :param encoding: the character set to first try to encode to; if None, will
                     use the system default (from the locale).
    :param desperate: if True and encoding to the given (or default) charset
                      fails, will also try utf-8 and latin-1 (in that order),
                      and if those fail, will encode to the preferred charset,
                      replacing unknown characters with \\uFFFD.
    :param coerce: if True, will coerce numeric types to a bytes object; if
                   False, such values will be returned untouched.
    :param fs: indicates value is a file name or other environment string; if True,
               the encoding (if not explicitly specified) will be the encoding
               given by ``sys.getfilesystemencoding()`` and the error handler
               used will be ``surrogateescape``.
    :returns: the value as a bytes object, or the original value if coerce is
              False and the value was not a bytes or string.
    """
    if isinstance(value, bytes):
        return value
    elif isinstance(value, (int, float)):
        return str(value).encode('ascii') if coerce else value
    elif not isinstance(value, str):
        # Need to coerce to a str before converting to bytes.
        value = tostr(value, coerce=coerce)

    errors = 'strict'
    if fs:
        encoding = encoding or sys.getfilesystemencoding()
        errors = 'surrogateescape'

    for c in (encoding or ENCODING, 'utf-8', 'latin-1'):
        try:
            return value.encode(c, errors)
        except UnicodeError:
            pass
        if not desperate:
            raise UnicodeError("Couldn't encode value to bytes (and not desperate enough to keep trying)")

    return value.encode(encoding or ENCODING, 'replace')


def tostr(value: Any, encoding: str | None = None, desperate: bool = True, coerce: bool = False,
          fs: bool = False) -> Any:
    """
    Convert (if necessary) the given value to a str.

    :param value: the value to be converted to a str
    :param encoding: the character set to first try to decode as; if None, will
                     use the system default (from the locale).
    :param desperate: if True and decoding to the given (or default) charset
                      fails, will also try utf-8 and latin-1 (in that order),
                      and if those fail, will decode as the preferred charset,
                      replacing unknown characters with \\uFFFD.
    :param coerce: if True, will coerce numeric types to a str; if False, such
                   values will be returned untouched.
    :param fs: indicates value is a file name or other environment string; if True,
               the encoding (if not explicitly specified) will be the encoding
               given by ``sys.getfilesystemencoding()`` and the error handler
               used will be ``surrogateescape``.
    :returns: the value as a str, or the original value if coerce is False and
              the value was not a bytes or string.
    """
    if value is None or isinstance(value, str):
        return value
    elif isinstance(value, (int, float)):
        return str(value) if coerce else value
    elif not isinstance(value, (bytearray, bytes)):
        return str(value)

    errors = 'strict'
    if fs:
        encoding = encoding or sys.getfilesystemencoding()
        errors = 'surrogateescape'

    for c in (encoding or ENCODING, 'utf-8', 'latin-1'):
        try:
            return value.decode(c, errors)
        except UnicodeError:
            pass
        if not desperate:
            raise UnicodeError("Couldn't decode value to str (and not desperate enough to keep trying)")

    return value.decode(encoding or ENCODING, 'replace')


def fsname(s: str | bytes | None) -> str | None:
    """
    Return an object appropriate to represent a filename, or None if the
    given value is None.

    :param s: the filename to decode (if needed)
    :returns: a string encoded with surrogateescape
    """
    return tostr(s, fs=True, desperate=False)


@functools.cache
def get_temp_path(appname: str) -> str:
    """
    Returns (creating if necessary) a private temporary directory for the
    current user and the given application name.
    """
    try:
        user: str | int = getpass.getuser()
    except Exception:
        user = os.getuid()
    path = os.path.join(_tempfile.gettempdir(), f'{appname}-{user}')
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass

    # Verify the directory is safe to use.
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f'Security Error: {path} is not a directory, aborted')
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise OSError(f'Security Error: {path} has wrong permissions, aborted')
    if st.st_uid != os.getuid():
        raise OSError(f'Security Error: {path} does not belong to you, aborted')
    return path


def tempfile(appname: str, name: str = '') -> str:
    """
    Return the path to a file with the given name in a secure temporary
    directory for the application.  Intermediate directories are created as
    needed.
    """
    path = os.path.join(get_temp_path(appname), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def daemonize(stdin: str = os.devnull, stdout: str = os.devnull, stderr: str | None = None,
              chdir: str | None = '/') -> None:
    """
    Does a double-fork to daemonize the current process.  The parent process
    exits immediately.
    """
    target_dir = chdir or os.getcwd()
    if os.fork() > 0:
        # Exit from the first parent.
        sys.exit(0)

    os.chdir(target_dir)
    os.setsid()

    if os.fork() > 0:
        # Exit from the second parent.
        sys.exit(0)

    # Remap standard file descriptors.
    with open(stdin, 'rb') as fin:
        os.dup2(fin.fileno(), sys.stdin.fileno())
    with open(stdout, 'ab') as fout:
        os.dup2(fout.fileno(), sys.stdout.fileno())
    with open(stderr or stdout, 'ab', 0) as ferr:
        os.dup2(ferr.fileno(), sys.stderr.fileno())
