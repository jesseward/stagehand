# Changelog

## Unreleased: modernization

Stagehand now runs on current Python (3.15) instead of being pinned to 3.3–3.6.

### Upgrading

* **Your config file and TV database are used as is.** The config format
  is unchanged (including scrambled passwords), and existing databases
  load, including search results pickled by older versions that refer to
  `kaa.base.dateutils`.
* **Python 3.15 or later is required.** Run the Docker image, or install with `uv tool install` or
  `pipx install` (see the README). The old bundled-zip, `setup.py` and
  Windows installs are gone.
* **Windows is no longer supported.** The system tray integration and
  Windows-specific paths were removed. Linux and macOS are supported.
* **`ffprobe` is now used to check downloads.** It replaces the bundled
  (GPL) kaa.metadata. Install FFmpeg to keep checking resolution, duration
  and audio language while downloading. Without it, downloads aren't checked
  and a warning is logged.
* **`stagehand --stop` now works with web authentication.** The control
  client speaks HTTP Digest; it used to send Basic auth, which the server
  rejected.

### Changed

* All bundled third-party code (`external/`) is gone: a pre-release asyncio
  (Tulip), aiohttp 0.9, BeautifulSoup and kaa.metadata. Dependencies are
  now only `aiohttp` and `bottle`, installed normally.
* The code uses native `async`/`await` throughout, replacing
  `@asyncio.coroutine`, `yield from` and `asyncio.async`, which were removed
  in Python 3.11, and the `stagehand.web.async` module, whose name became a
  reserved word in 3.7.
* The web server is now `aiohttp.web`, replacing Bottle run through a WSGI
  adapter. Pages still use Bottle's `SimpleTemplate`.
* The web UI no longer uses jQuery 1.7, jQuery UI 1.8 or its six plugins,
  all from 2012. It's now plain JavaScript and CSS with no libraries, using
  `fetch()`, native `<dialog>` and `<progress>`, and CSS toggle switches.
  CoffeeScript is gone too, so there is no build step. The pages look
  mostly the same; jQuery UI-themed widgets are restyled.
* An unused, half-finished second UI (`data/web/new/`, with jQuery 2 and
  Semantic UI) was removed.
* Configuration schemas (`*.cxml`) are loaded at runtime instead of being
  turned into Python code at build time.
* Packaging uses `pyproject.toml` (hatchling). The version comes from git
  tags, and static data ships inside the package.
* Easynews results are parsed as XML with the standard library.
* The HTTP downloader uses one shared aiohttp session with connection
  pooling. It resumes partial files with Range requests and retries server
  errors with backoff.
* Code is type-annotated (`py.typed`), checked with mypy, and linted with
  ruff.
* Added a test suite of about 140 tests covering the TV database, providers,
  searchers, retrieval, the web interface and API, the config format,
  database compatibility, and startup and shutdown.

### Fixed

* Series names from search results and provider-supplied episode overviews
  were inserted into pages as HTML, so a crafted name could run script in
  the browser. Notifications are now plain text unless explicitly marked as
  HTML, and data embedded in pages is escaped for `<script>` blocks.

* TVmaze air times were never parsed (wrong time format and a missing
  import), and TVmaze search results showed no poster.
* Episodes with no file reported their filename as the string `'None'`.
* `Season` objects could not be hashed (`hash()` was called with two
  arguments).
* Setting a searchable database attribute to `None` was silently ignored.
  As a result, failed or cancelled downloads left the episode pointing at a
  deleted file.
* The series cache built new `Series` objects on every rebuild instead of
  reusing existing ones, so references held elsewhere went stale after a
  metadata refresh.
* Finished background series updates were never removed from tracking,
  which produced spurious "BUG: … unexpectedly active" warnings.
* Changing `retrievers.parallel` crashed with a reference to a nonexistent
  method, instead of starting more downloads.
* Reloading the config never ran the series reload. It was an un-awaited
  coroutine.
* Episode-matching errors were logged with the wrong arguments, and the
  error path crashed on Python 3 (`dict.values() + dict.values()`).
* Adding a series from a search result returned a generator instead of
  the series.
* Logic errors in date parsing (RFC 822 dates with out-of-range fields)
  and in the inotify watcher.
* XBMC/Kodi library paths were remapped incorrectly, and connections
  weren't closed.
* The email notifier now uses the modern `email` API and re-sends EHLO after
  STARTTLS.
