# Stagehand

**This software is somewhat half-baked. It only works (though it works well)
if you have an [Easynews](https://easynews.com) account.  Generic NNTP isn't
supported yet (but help is welcome).**


## What it is

Stagehand is a manager for your favourite TV series.  It automatically
downloads new episodes of the TV shows in your library, and provides a convenient
interface to download previously aired episodes.

Here are some of the main features:

* ~~Pretty, modern-looking UI~~ (Well it was 10 years ago.)
* Support for multiple TV metadata providers (currently TheTVDB and TVmaze): easily choose the authoritative provider per-series
* (Exclusive) support for Easynews HTTP-based global search
* Runs in Docker, or anywhere with Python 3.15 (Linux and macOS; Windows is no longer supported)

## What it isn't

The core of Stagehand is quite robust, but many essential features are missing:

* NZB and NNTP support (for non-Easynews Usenet services): the most critical missing functionality
* Bittorrent
* Web-based configuration UI
* Ability to import an existing TV library
* ... and a bazillion FIXMEs and TODOs in the source


## What it looks like

![](https://stagehand.ca/img/stagehand.jpg)

![](https://stagehand.ca/img/stagehand2.jpg)



## How to run it

The simplest way is Docker.  Change `/data/tv` below to the path where you want
to hold downloaded episodes:

```bash
docker build --build-arg VERSION=$(git describe --tags) -t stagehand .
docker run -d --name stagehand -u $(id -u):$(id -g) --net=host \
    -v $HOME:/stagehand -v /data/tv:/stagehand/tv stagehand
docker logs -f stagehand
```

If things are working properly, you should see output that looks like this:

```
2026-11-20 04:27:26,640 [INFO] stagehand: starting Stagehand 0.4.0
2026-11-20 04:27:26,641 [INFO] manager: watching /stagehand/.config/stagehand/config for changes
2026-11-20 04:27:26,642 [INFO] web: started webserver at http://myhost:8088/
2026-11-20 04:27:26,643 [INFO] manager: scheduling next episode check for 2026-11-20 05:06:00
2026-11-20 04:27:26,644 [INFO] manager: no new episodes; we are all up to date
2026-11-20 04:27:26,644 [INFO] manager: stagehand started, waiting for next new episodes check
```

Note the webserver URL in the output above.  You should be able to browse to this URL from
within your network.  Before proceeding with configuration, ensure that it's reachable.

The image includes `ffprobe` (from [FFmpeg](https://ffmpeg.org/)), which Stagehand uses
to verify downloads while they're in progress (resolution, duration and audio language),
aborting ones that don't meet a series' requirements.  Add `--build-arg FFMPEG=0` to build
a much smaller image without it, in which case downloads aren't verified.

Logs are written to `~/.cache/stagehand/logs/`, and the config file and database live in
`~/.config/stagehand/` (inside the volume mounted at `/stagehand`).  Send `SIGHUP` to reload
the config (`docker kill -s HUP stagehand`), or `SIGUSR1` to force a refresh of TV metadata.

### Without Docker

Stagehand requires Python 3.15.  Install it as a command line tool with
[uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/), and optionally install
FFmpeg for download verification:

```bash
uv tool install git+https://github.com/jtackaberry/stagehand
stagehand            # -b to run in the background, --stop to stop it, -v for more logging
```


## How to configure it

Ideally you'd be able to configure Stagehand from the web interface, but this isn't
implemented yet. Until then, you will need to edit the config file at
`~/.config/stagehand/config`.

Minimally, you will need these lines, which you can safely append to the bottom
of the file:

```
searchers.enabled[+] = easynews
searchers.easynews.username = your_easynews_username
searchers.easynews.password = your_easynews_password
```

Once you save the config file, you're ready to start using Stagehand.  No reload
is needed, it will pick up the changes dynamically.


## Development

```bash
uv venv -p 3.15 && uv pip install -e '.[dev,browser]'
playwright install chromium              # for the browser tests (skipped if not installed)
pytest                                   # run the test suite
ruff check stagehand tests               # lint
mypy stagehand                           # type check
python -m stagehand -vv                  # run from the source tree (templates reload on change)
```

The web interface is served by [aiohttp](https://docs.aiohttp.org/), with
pages rendered from Bottle `SimpleTemplate` templates in
`stagehand/data/web/`.  The client side is plain JavaScript and CSS with no
libraries (`js/stagehand.js`, `css/stagehand.css`); `tests/test_browser.py`
drives it with Playwright.  Everything is `asyncio`-based; long-running API calls
are run as background jobs that the browser polls (see `stagehand/web/jobs.py`).

See [CHANGELOG.md](CHANGELOG.md) for what changed in the move to modern Python.
