import os
import sys
import time
import lzma
import logging
import logging.config
import asyncio
import warnings
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count
from typing import Set

import raven
import diskcache
from sanic import Sanic
from sanic import response
from sanic.exceptions import NotFound
from raven_aiohttp import AioHttpTransport

from .prerender import Prerender, CONCURRENCY_PER_WORKER, TemporaryBrowserFailure


logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=cpu_count() * 5)

ALLOWED_DOMAINS: Set = set(dm.strip() for dm in os.environ.get('PRERENDER_ALLOWED_DOMAINS', '').split(',') if dm.strip())
CACHE_ROOT_DIR: str = os.environ.get('CACHE_ROOT_DIR', '/tmp/prerender')
CACHE_LIVE_TIME: int = int(os.environ.get('CACHE_LIVE_TIME', 3600))
SENTRY_DSN = os.environ.get('SENTRY_DSN')

cache = diskcache.Cache(CACHE_ROOT_DIR)
if SENTRY_DSN:
    sentry = raven.Client(
        SENTRY_DSN,
        transport=AioHttpTransport,
        release=raven.fetch_package_version('prerender'),
        site='Prerender',
    )
else:
    sentry = None


async def _fetch_from_cache(key: str) -> str:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, cache.get, key)
    if data is not None:
        res = await loop.run_in_executor(None, lzma.decompress, data)
        return res


def _save_to_cache(key, html: bytes) -> None:
    try:
        compressed = lzma.compress(html)
        cache.set(key, compressed, expire=CACHE_LIVE_TIME)
    except Exception:
        logger.exception('Error writing cache')


app = Sanic(__name__)
app.config.from_object(dict(
    KEEP_ALIVE=False,
))


@app.route('/browser/list')
async def list_browser_pages(request):
    renderer = request.app.prerender
    pages = await renderer.pages()
    return response.json(pages, ensure_ascii=False, indent=2, escape_forward_slashes=False)


@app.route('/browser/version')
async def show_brower_version(request):
    renderer = request.app.prerender
    version = await renderer.version()
    return response.json(version, ensure_ascii=False, indent=2, escape_forward_slashes=False)


@app.route('/browser/disable', methods=['PUT'])
async def disable_browser_rendering(request):
    global CONCURRENCY_PER_WORKER

    CONCURRENCY_PER_WORKER = 0
    return response.json({'message': 'success'})


@app.route('/browser/enable', methods=['PUT'])
async def enable_browser_rendering(request):
    global CONCURRENCY_PER_WORKER

    CONCURRENCY_PER_WORKER = int(os.environ['CONCURRENCY'])
    return response.json({'message': 'success'})


async def _render(prerender: Prerender, url: str, format: str = 'html') -> str:
    '''Retry once after TemporaryBrowserFailure occurred.'''
    for i in range(2):
        try:
            return await prerender.render(url, format)
        except TemporaryBrowserFailure as e:
            if i < 1:
                logger.warning('Temporary browser failure: %s, retry rendering %s in 1s', str(e), url)
                await asyncio.sleep(1)
                continue
            raise


@app.exception(NotFound)
async def handle_request(request, exception):
    # compatible with Sanic 0.4.1+
    format = 'html'
    url = getattr(request, 'path', request.url)
    if url.startswith('/http'):
        url = url[1:]
    elif url.startswith('/mhtml/http'):
        format = 'mhtml'
        url = url[7:]
    elif url.startswith('/pdf/http'):
        format = 'pdf'
        url = url[5:]
    if request.query_string:
        url = url + '?' + request.query_string
    parsed_url = urlparse(url)

    if not parsed_url.hostname:
        return response.text('Bad Request', status=400)

    if ALLOWED_DOMAINS:
        if parsed_url.hostname not in ALLOWED_DOMAINS:
            return response.text('Forbiden', status=403)

    cache_key = url
    if format != 'html':
        cache_key += format
    try:
        data = await _fetch_from_cache(cache_key)
        if data is not None:
            logger.info('Got 200 for %s in cache', url)
            if format == 'html':
                return response.html(data.decode('utf-8'), headers={'X-Prerender-Cache': 'hit'})
            return response.raw(data, headers={'X-Prerender-Cache': 'hit'})
    except Exception:
        logger.exception('Error reading cache')

    if CONCURRENCY_PER_WORKER <= 0:
        # Read from cache only
        logger.warning('Got 502 for %s, prerender unavailable', url)
        return response.text('Bad Gateway', status=502)

    start_time = time.time()
    try:
        data = await _render(request.app.prerender, url, format)
        duration_ms = int((time.time() - start_time) * 1000)
        logger.info('Got 200 for %s in %dms', url, duration_ms)
        if format == 'html':
            executor.submit(_save_to_cache, cache_key, data.encode('utf-8'))
            return response.html(data, headers={'X-Prerender-Cache': 'miss'})
        executor.submit(_save_to_cache, cache_key, data)
        return response.raw(data, headers={'X-Prerender-Cache': 'miss'})
    except (asyncio.TimeoutError, asyncio.CancelledError, TemporaryBrowserFailure):
        duration_ms = int((time.time() - start_time) * 1000)
        logger.warning('Got 504 for %s in %dms', url, duration_ms)
        return response.text('Gateway timeout', status=504)
    except Exception:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.exception('Internal Server Error for %s in %dms', url, duration_ms)
        if sentry:
            sentry.captureException()
        return response.text('Internal Server Error', status=500)


@app.listener('before_server_start')
async def before_server_start(app: Sanic, loop):
    loop.set_default_executor(executor)

    logging_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'root': {
            'handlers': ['console'],
            'level': 'DEBUG' if app.debug else 'INFO'
        },
        'handlers': {
            'console': {
                'level': 'DEBUG' if app.debug else 'INFO',
                'class': 'logging.StreamHandler',
                'formatter': 'default',
                'stream': sys.stderr,
            }
        },
        'formatters': {
            'default': {
                'format': '%(asctime)s %(levelname)-2s %(name)s.%(funcName)s:%(lineno)-5d %(message)s',  # NOQA
            },
        },
    }
    logging.config.dictConfig(logging_config)
    if app.debug or loop.get_debug():
        warnings.simplefilter('always', ResourceWarning)

    app.prerender = Prerender(loop=loop)
    if CONCURRENCY_PER_WORKER > 0:
        try:
            await app.prerender.bootstrap()
        except Exception:
            logger.error('Error bootstrapping Prerender, please start Chrome first.')
            await app.prerender.shutdown()
            raise


@app.listener('after_server_stop')
async def after_server_stop(app: Sanic, loop):
    await app.prerender.shutdown()
