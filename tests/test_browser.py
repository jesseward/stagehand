"""
Browser tests of the web interface, using Playwright with headless Chromium.

These exercise the JavaScript client (API calls, background job polling,
notifications, and the interactive widgets) against a real server.  They are
skipped unless Playwright and its Chromium build are installed:

    pip install -e '.[browser]' && playwright install chromium
"""
import asyncio

import pytest

from stagehand.tvdb import Episode
from stagehand.web import create_app
from stagehand.web.jobs import asyncweb

playwright_api = pytest.importorskip('playwright.async_api')


@pytest.fixture
async def browser():
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except playwright_api.Error as e:
            pytest.skip(f'Chromium not available: {e}')
        yield browser
        await browser.close()


@pytest.fixture
async def show(manager):
    assert await manager.add_series('tvmaze:100') is not None
    return manager.tvdb.get_series_by_id('tvmaze:100')


@pytest.fixture
async def server(aiohttp_server, manager):
    return await aiohttp_server(create_app(manager, manager.paths.data))


@pytest.fixture
async def page(browser, server):
    """A browser page that fails the test on any JavaScript error."""
    context = await browser.new_context(base_url=str(server.make_url('')))
    page = await context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(f'page error: {e}'))
    page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
    page.errors = errors
    yield page
    await context.close()
    assert errors == []


expect = playwright_api.expect


async def eventually(fn, expected):
    """Waits up to 5s until fn() returns the expected value (e.g. server-side state)."""
    async with asyncio.timeout(5):
        while fn() != expected:  # noqa: ASYNC110 -- polling state that has no event
            await asyncio.sleep(0.05)


PAGES = ['/', '/tv/', '/tv/add', '/tv/upcoming', '/tv/tvmaze:100', '/downloads/', '/settings/']


@pytest.mark.parametrize('path', PAGES)
async def test_pages_load_without_errors(page, show, path):
    failed = []
    page.on('response', lambda r: r.status >= 400 and failed.append(r.url))
    await page.goto(path)
    await page.wait_for_load_state('networkidle')
    assert failed == []
    assert await page.evaluate('typeof jQuery') == 'undefined'
    if path == '/tv/tvmaze:100':
        # The test show has no banner, so no (broken) banner image is shown.
        assert await page.locator('img[src$="/banner"]').count() == 0


async def test_add_series(page, manager):
    await page.goto('/tv/add')
    add_button = page.locator('#button-add')
    await expect(add_button).to_be_disabled()

    await page.fill('#series-name', 'Test Show')
    await page.press('#series-name', 'Enter')
    menu = page.locator('#select-series')
    await expect(menu.locator('option[value="thetvdb:200"]')).to_have_count(1)
    # Search results are rendered as text, not HTML.
    assert await menu.locator('option[value="thetvdb:200"]').text_content() == 'Test Show (2020-01-05)'

    await menu.select_option('thetvdb:200')
    await expect(page.locator('#select-series-link')).to_be_visible()
    await add_button.click()
    await page.wait_for_url('**/tv/thetvdb:200')
    await expect(page.locator('h1').first).to_contain_text('Test Show')
    assert manager.tvdb.get_series_by_id('thetvdb:200') is not None


async def test_search_with_no_results(page, manager, monkeypatch):
    async def search(name, provider='thetvdb'):
        return []
    monkeypatch.setattr(manager.tvdb, 'search', search)
    await page.goto('/tv/add')
    await page.fill('#series-name', 'Nothing')
    await page.click('#button-search')
    await expect(page.locator('.toast[data-type=error]')).to_contain_text('No TV series were found')


async def test_library_sort_and_navigate(page, show):
    await page.goto('/tv/')
    rows = page.locator('#series-list tbody tr')
    await expect(rows).to_have_count(1)
    header = page.locator('#series-list th').first
    await header.click()
    await expect(header).to_have_attribute('aria-sort', 'ascending')
    await header.click()
    await expect(header).to_have_attribute('aria-sort', 'descending')
    await rows.first.locator('td').nth(2).click()
    await page.wait_for_url('**/tv/tvmaze:100')


async def test_show_settings_are_saved(page, show):
    await page.goto('/tv/tvmaze:100')
    await page.click('label[for=series-quality-SD]')
    await eventually(lambda: str(show.cfg.quality), 'SD')

    await page.click('#series-paused')
    await eventually(lambda: bool(show.cfg.paused), True)
    await expect(page.locator('#status-icon-paused')).to_be_visible()
    await expect(page.locator('#status-icon-notpaused')).to_be_hidden()

    await page.click('#advanced summary')
    await page.fill('#series-path', 'Elsewhere')
    await page.press('#series-path', 'Tab')
    await eventually(lambda: str(show.cfg.path), 'Elsewhere')


async def test_change_episode_status(page, show):
    await page.goto('/tv/tvmaze:100')
    bar = page.locator('#episode-button-bar')
    await expect(bar).not_to_have_class('open')
    row = page.locator('[id="tvmaze:100-s01e01"]')
    await row.locator('input[type=checkbox]').check()
    await expect(bar).to_have_class('open')

    await bar.locator('button[data-status=ignore]').click()
    await expect(row.locator('td:last-child img')).to_have_attribute('src', '/static/img/icon-20-status-ignore.png')
    await expect(bar).not_to_have_class('open')
    assert show.get_episode_by_code('s01e01').status == Episode.STATUS_IGNORE


async def test_select_season(page, show):
    await page.goto('/tv/tvmaze:100')
    await page.locator('tr.heading input[type=checkbox]').first.check()
    checked = page.locator('tr.ep input[type=checkbox]:checked')
    await expect(checked).to_have_count(3)
    # The header checkbox selects, then deselects, everything.
    header = page.locator('thead input[type=checkbox]')
    await header.check()
    await expect(checked).to_have_count(await page.locator('tr.ep').count())
    await header.uncheck()
    await expect(checked).to_have_count(0)


async def test_episode_overview_tooltip(page, show):
    await page.goto('/tv/tvmaze:100')
    await page.locator('[id="tvmaze:100-s01e02"] td').nth(2).click()
    tip = page.locator('.tip')
    await expect(tip).to_be_visible()
    await expect(tip).to_have_text('Second.')
    await page.locator('h1').first.click()
    await expect(tip).to_be_hidden()


async def test_delete_series(page, show, manager):
    await page.goto('/tv/tvmaze:100')
    await page.click('#button-delete')
    dialog = page.locator('dialog.confirm')
    await expect(dialog).to_be_visible()
    await dialog.locator('button[value=cancel]').click()
    await expect(dialog).to_have_count(0)
    assert manager.tvdb.get_series_by_id('tvmaze:100') is not None

    await page.click('#button-delete')
    await dialog.locator('button[value=confirm]').click()
    await page.wait_for_url('**/tv/')
    assert manager.tvdb.get_series_by_id('tvmaze:100') is None
    # The deletion notice arrives via job polling on the next page.
    await expect(page.locator('.toast')).to_contain_text('was removed from the database', timeout=15000)


async def test_download_needed_episodes(page, show):
    await page.goto('/tv/tvmaze:100')
    await page.click('#button-search')
    toast = page.locator('.toast', has_text='Episode check')
    await expect(toast).to_contain_text('series', timeout=10000)
    await expect(page.locator('#button-search')).to_be_enabled()


async def test_server_alerts_are_text(page, show):
    await page.goto('/')
    # Polling starts at a 5s interval; deliver the alert on the next poll.
    asyncweb.notify('alert', title='Hello', text='<img src=x onerror=alert(1)> plain')
    toast = page.locator('.toast', has_text='Hello')
    await expect(toast).to_be_visible(timeout=15000)
    await expect(toast.locator('.toast-body')).to_have_text('<img src=x onerror=alert(1)> plain')
    assert await toast.locator('img').count() == 0


async def test_downloads_filter(page, show):
    await page.goto('/downloads/')
    await page.select_option('#select-weeks', '4')
    await page.wait_for_url('**/downloads/?status=have&weeks=4')
    await expect(page.locator('#select-weeks')).to_have_value('4')


async def test_download_progress_indicator(page, show, manager):
    await page.goto('/')
    indicator = page.locator('#banner #progress')
    await expect(indicator).to_be_hidden()
    ep = show.get_episode_by_code('s01e01')
    asyncweb.notify('dlprogress', queue=[[show.id, ep.code, [42, '1.0', '2.4', 100]]], replace=True, universal=True)
    await expect(indicator).to_be_visible(timeout=15000)
    await expect(indicator.locator('span')).to_have_attribute('style', 'width: 42%;')
