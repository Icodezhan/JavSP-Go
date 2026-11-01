import json
import os
import sys

import lxml.html
import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.cookiecloud import cookies_for_url
import javsp.cookiecloud as cookiecloud
from javsp.datatype import MovieInfo
from javsp.web.base import Request
from javsp.web.exceptions import MovieNotFoundError, SiteBlocked, WebsiteError
from javsp.web.mirrors import get_with_mirror_fallback
from javsp.__main__ import crawl_with_retry
from javsp.web import jav321, javbus


def _reset_cookie_cache():
    cookiecloud._cached_path = None
    cookiecloud._cached_mtime = None
    cookiecloud._cached_cookies = {}


class _Resp:
    def __init__(self, text, url, status_code=200, history=None):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.history = history or []
        self.content = text.encode('utf-8')
        self.encoding = 'utf-8'


FILM_HTML = """
<html><head><title>ABC-123</title></head><body>
<div class="container">
  <h3>ABC-123 标题</h3>
  <a class="bigImage"><img src="https://img.example/cover.jpg"/></a>
  <div id="sample-waterfall"></div>
  <div class="col-md-3 info">
    <p><span>識別碼:</span><span>ABC-123</span></p>
    <p><span>發行日期:</span>2020-01-02</p>
    <p><span>長度:</span>120分鐘</p>
  </div>
</div>
</body></html>
"""


def test_cookies_for_url_without_env(monkeypatch):
    monkeypatch.delenv('JAVSP_COOKIECLOUD_FILE', raising=False)
    _reset_cookie_cache()
    assert cookies_for_url('https://javdb.com/search') == {}


def test_cookies_for_url_matches_domain(tmp_path, monkeypatch):
    cookie_file = tmp_path / 'cookies.json'
    cookie_file.write_text(json.dumps({
        '.javdb.com': {'session': 'abc', 'overridden': 'from-file'},
        'example.com': {'other': '1'},
    }), encoding='utf-8')
    monkeypatch.setenv('JAVSP_COOKIECLOUD_FILE', str(cookie_file))
    _reset_cookie_cache()
    matched = cookies_for_url('https://www.javdb.com/v/1')
    assert matched['session'] == 'abc'
    assert 'other' not in matched


def test_mirror_ssl_error_falls_back_to_canonical():
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if 'mirror.example' in url:
            raise requests.exceptions.SSLError('bad cert')
        return 'ok'

    result = get_with_mirror_fallback(
        get,
        'https://mirror.example/ABC-123?x=1',
        'https://mirror.example',
        'https://www.javbus.com',
        delay_raise=True,
    )
    assert result == 'ok'
    assert calls == [
        'https://mirror.example/ABC-123?x=1',
        'https://www.javbus.com/ABC-123?x=1',
    ]


def test_mirror_http_error_and_timeout_do_not_fall_back():
    def get(url, **kwargs):
        raise requests.exceptions.HTTPError('403')

    with pytest.raises(requests.exceptions.HTTPError):
        get_with_mirror_fallback(
            get, 'https://mirror.example/ABC-123', 'https://mirror.example', 'https://www.javbus.com'
        )

    def timeout(url, **kwargs):
        raise requests.exceptions.Timeout('timed out')

    with pytest.raises(requests.exceptions.Timeout):
        get_with_mirror_fallback(
            timeout, 'https://mirror.example/ABC-123', 'https://mirror.example', 'https://www.javbus.com'
        )


def test_scraper_reraises_ssl_error():
    request = Request(use_scraper=False)

    def boom(*args, **kwargs):
        raise requests.exceptions.SSLError('cert')

    with pytest.raises(requests.exceptions.SSLError):
        request._scraper_monitor(boom)('https://javdb.com')


def _patch_javbus(monkeypatch, pages):
    class FakeRequest:
        def __init__(self):
            self.headers = {}
            self.calls = []

        def get(self, url, delay_raise=False):
            self.calls.append(('get', url))
            return pages[0]

        def post(self, url, data, delay_raise=False):
            self.calls.append(('post', data))
            return pages[1]

    holder = {}

    def new_request():
        holder['request'] = FakeRequest()
        return holder['request']

    def fallback(get, url, mirror, canonical, **kwargs):
        return get(url, **kwargs)

    monkeypatch.setattr(javbus, 'new_request', new_request)
    monkeypatch.setattr(javbus, 'get_with_mirror_fallback', fallback)
    return holder


def test_javbus_age_gate_posts_confirmation(monkeypatch):
    age = _Resp('<html><title>Age Verification</title></html>', 'https://www.seedmm.help/ABC-123')
    film = _Resp(FILM_HTML, 'https://www.seedmm.help/ABC-123')
    holder = _patch_javbus(monkeypatch, (age, film))
    movie = MovieInfo('ABC-123')
    javbus.parse_data(movie)
    assert holder['request'].calls[1] == ('post', {'Submit': '確認'})
    assert movie.title == '标题'
    assert movie.duration == '120'


def test_javbus_empty_body_after_confirm_is_site_blocked(monkeypatch):
    age = _Resp('<html><title>Age Verification</title></html>', 'https://www.seedmm.help/ABC-123')
    empty = _Resp('   ', 'https://www.seedmm.help/ABC-123')
    _patch_javbus(monkeypatch, (age, empty))
    with pytest.raises(SiteBlocked, match='响应为空'):
        javbus.parse_data(MovieInfo('ABC-123'))


def test_javbus_age_gate_remains_site_blocked(monkeypatch):
    age = _Resp('<html><title>Age Verification</title></html>', 'https://www.seedmm.help/ABC-123')
    _patch_javbus(monkeypatch, (age, age))
    with pytest.raises(SiteBlocked, match='仍是验证页'):
        javbus.parse_data(MovieInfo('ABC-123'))


def test_jav321_empty_dropdown_is_not_found(monkeypatch):
    html = lxml.html.fromstring('<html><body><ul class="dropdown-menu"></ul></body></html>')
    monkeypatch.setattr(jav321, 'post_html', lambda *args, **kwargs: html)
    with pytest.raises(MovieNotFoundError, match='IPX-177'):
        jav321.parse_data(MovieInfo('IPX-177'))


def test_http_403_does_not_retry():
    calls = {'n': 0}

    def parser(info):
        calls['n'] += 1
        response = requests.Response()
        response.status_code = 403
        error = requests.HTTPError('403 Client Error')
        error.response = response
        raise error

    info = MovieInfo('ABC-123')
    crawl_with_retry(parser, info, 3, 'javsp.web.javbus')
    assert calls['n'] == 1
    assert not hasattr(info, 'success')


def test_timeout_still_retries_and_website_error_stops():
    timeouts = {'n': 0}

    def parser(info):
        timeouts['n'] += 1
        raise requests.exceptions.Timeout('timed out')

    crawl_with_retry(parser, MovieInfo('ABC-123'), 3, 'javsp.web.javbus')
    assert timeouts['n'] == 3

    blocked = {'n': 0}

    def website_error(info):
        blocked['n'] += 1
        raise WebsiteError('unexpected')

    crawl_with_retry(website_error, MovieInfo('ABC-123'), 3, 'javsp.web.javdb')
    assert blocked['n'] == 1
