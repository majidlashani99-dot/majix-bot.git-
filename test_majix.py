import io
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch
import majix
import telegram_client


class Tests(unittest.TestCase):
    def test_classification(self):
        g, r, w = (30, 180, 40), (200, 40, 40), (240, 240, 240)
        self.assertEqual(majix.classify(g, r, g)[0], 'LONG')
        self.assertEqual(majix.classify(r, r, g)[0], 'SHORT')
        for colors in [(w, r, g), (g, w, g), (g, r, w), ((0, 0, 0), r, g)]:
            self.assertIsNone(majix.classify(*colors)[0])
        self.assertFalse(majix.green((75, 100, 0)))
        self.assertFalse(majix.red((130, 0, 0)))

    def test_parse_title(self):
        self.assertEqual(majix.parse_title('BTCUSDT.P 84,919.8 ▲ +0.25% Unnamed - Google Chrome'),
                         ('BTCUSDT.P', '84,919.8'))
        self.assertEqual(majix.parse_title('TradingView'), (None, None))

    def test_levels(self):
        self.assertEqual(majix.parse_price('0.09345 (-0.25%)'), '0.09345')
        self.assertIsNone(majix.parse_price('abc'))
        short = dict(entry='0.09322', sl='0.09345', tp1='0.09284', tp2='0.09246')
        self.assertTrue(majix.levels_ok('SHORT', short))
        self.assertFalse(majix.levels_ok('LONG', short))
        self.assertFalse(majix.levels_ok('SHORT', dict(short, tp2=None)))
        self.assertFalse(majix.levels_ok('SHORT', dict(short, sl='0.0345')))

    def test_message_format(self):
        from datetime import datetime
        lv = dict(entry='0.09322', sl='0.09345', tp1='0.09284', tp2='0.09246')
        h, plain = majix.build_message('DOGEUSDT.P', 'SHORT', '0.09314', lv, '', datetime(2026, 10, 4, 11, 21, 42))
        for part in ('SHORT', 'DOGEUSDT.P', '0.09322', '0.09345', '(-0.25%)', '(+0.41%)', '(+0.82%)', '0.09314'):
            self.assertIn(part, plain)
        self.assertIn('<b>', h)
        self.assertNotIn('<', plain)
        _, p2 = majix.build_message('X', 'LONG', None, None, 'Tesseract نصب نیست', datetime(2026, 1, 1))
        self.assertIn('Tesseract', p2)
        self.assertIn('خرید', p2)

    def test_mode_validation(self):
        c = dict(majix.DEFAULTS, token='123:abc', chat_id='456')
        for mode in ('auto', 'direct', 'system'):
            majix.validate(dict(c, mode=mode))
        majix.validate(dict(c, mode='direct', proxy=''))
        with self.assertRaises(ValueError):
            majix.validate(dict(c, mode='bogus'))
        with self.assertRaises(ValueError):
            majix.validate(dict(c, mode='proxy', proxy=''))

    def test_auto_falls_back_from_dead_proxy_to_direct(self):
        calls = []
        def make(config, route=None):
            def opener(req, timeout):
                calls.append(route)
                if route == 'proxy':
                    raise URLError('proxy down')
                return io.BytesIO(b'{"ok":true}')
            return opener
        c = dict(majix.DEFAULTS, token='123:s', chat_id='456', mode='auto', proxy='10.0.0.1:8080')
        with patch('telegram_client.make_opener', make), patch('telegram_client.request.getproxies', lambda: {}):
            telegram_client._last_good.clear()
            self.assertTrue(majix.send_telegram(c, 'hi', threading.Event()))
            self.assertEqual(calls, ['proxy', 'direct'])
            calls.clear()  # next call goes straight to the route that worked
            self.assertTrue(majix.send_telegram(c, 'hi', threading.Event()))
            self.assertEqual(calls, ['direct'])

    def test_direct_mode_ignores_proxy(self):
        c = dict(majix.DEFAULTS, mode='direct', proxy='10.0.0.1:8080')
        self.assertEqual(telegram_client.route_list(c), ['direct'])
        with patch('telegram_client.request.build_opener') as build:
            telegram_client.make_opener(c, 'direct')
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_parse_mode_only_when_requested(self):
        c = dict(majix.DEFAULTS, token='123:s', chat_id='456')
        seen = []
        def opener(req, timeout):
            seen.append(json.loads(req.data))
            return io.BytesIO(b'{"ok":true}')
        majix.send_telegram(c, 'x', threading.Event(), opener, parse_mode='HTML')
        self.assertEqual(seen[0]['parse_mode'], 'HTML')

    def test_multipart_and_send_photo(self):
        c = dict(majix.DEFAULTS, token='123:s', chat_id='456')
        seen = []
        def opener(req, timeout):
            seen.append((req.full_url, req.get_header('Content-type'), req.data, timeout))
            return io.BytesIO(b'{"ok":true}')
        self.assertTrue(telegram_client.send_photo(c, b'\xff\xd8JPEG', '<b>سیگنال</b>', threading.Event(),
                                                  opener, parse_mode='HTML'))
        url, ctype, body, timeout = seen[0]
        self.assertTrue(url.endswith('/sendPhoto'))
        self.assertTrue(ctype.startswith('multipart/form-data; boundary='))
        boundary = ctype.split('boundary=')[1].encode()
        self.assertIn(b'name="photo"; filename="chart.jpg"', body)
        self.assertIn(b'\xff\xd8JPEG', body)
        self.assertIn('سیگنال'.encode(), body)
        self.assertIn(b'name="parse_mode"', body)
        self.assertTrue(body.endswith(b'--' + boundary + b'--\r\n'))
        self.assertGreaterEqual(timeout, 30)

    def test_crop_box(self):
        self.assertEqual(majix.crop_box([56, 90, 1070, 668], (1366, 768)), (56, 90, 1070, 668))
        self.assertEqual(majix.crop_box([56, 90, 5000, 5000], (1366, 768)), (56, 90, 1366, 768))
        self.assertEqual(majix.crop_box([5, 5, 6, 6], (1366, 768)), (0, 0, 1366, 768))
        self.assertEqual(majix.crop_box(None, (1366, 768)), (0, 0, 1366, 768))

    def test_shot_validation(self):
        c = dict(majix.DEFAULTS, token='123:abc', chat_id='456')
        majix.validate(c)
        for bad in ([0, 0, 10], [100, 100, 50, 50], [0, 0, 5000, 700]):
            with self.assertRaises(ValueError):
                majix.validate(dict(c, shot=bad))

    def _scan_with_photo(self, photo_error=None):
        class Stop:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, delay): return self.stopped
        stop, calls = Stop(), []
        class Desktop:
            title, size = 'x', (1366, 768)
            def __init__(self, c): pass
            def sample(self):
                return [(20, 180, 20), (200, 20, 20), (20, 180, 20)]
            def move(self, *a): pass
            def read_title(self): return 'DOGEUSDT.P 0.09314 ▲ +1% Chrome'
            def read_levels(self): return dict(entry='0.09322', sl='0.09345', tp1='0.09284', tp2='0.09246')
            def screenshot(self):
                stop.stopped = True  # end the scan after this alert is sent
                return b'JPEGDATA'
        def fake_call(config, method, payload, stop_, opener=None, emit=None, files=None):
            calls.append((method, payload, files))
            if method == 'sendPhoto' and photo_error:
                raise photo_error
            return {'ok': True}
        c = dict(majix.DEFAULTS, token='123:s', chat_id='456', count=1, sound=False)
        with patch('telegram_client.telegram_call', fake_call):
            majix.scan(c, stop, lambda *a: None, Desktop)
        return calls

    def test_scan_sends_photo_with_caption(self):
        calls = self._scan_with_photo()
        self.assertEqual([m for m, _, _ in calls], ['sendPhoto'])
        method, payload, files = calls[0]
        self.assertIn('DOGEUSDT.P', payload['caption'])
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertEqual(files['photo'][1], b'JPEGDATA')

    def test_photo_rejected_falls_back_to_text(self):
        err = telegram_client.TelegramError('bad photo')
        err.code = 400
        calls = self._scan_with_photo(err)
        self.assertEqual([m for m, _, _ in calls], ['sendPhoto', 'sendMessage'])
        self.assertIn('DOGEUSDT.P', calls[1][1]['text'])

    def test_dedup_reactivation_and_change(self):
        d = majix.Dedup(60)
        self.assertTrue(d.needed(1, 'LONG', 0))
        d.mark(1, 'LONG', 0)
        self.assertFalse(d.needed(1, 'LONG', 59))
        self.assertTrue(d.needed(1, 'LONG', 60))
        self.assertTrue(d.needed(1, 'SHORT', 2))
        self.assertFalse(d.needed(1, None, 3))
        self.assertTrue(d.needed(1, 'LONG', 4))

    def test_validation(self):
        c = dict(majix.DEFAULTS, token='123:abc', chat_id='-456')
        majix.validate(c)
        for changes in [dict(wait=float('nan')), dict(signal=[0, 2]), dict(count=0), dict(symbols='BTC')]:
            with self.assertRaises(ValueError):
                majix.validate(dict(c, **changes))

    def test_telegram_payload(self):
        c = dict(majix.DEFAULTS, token='123:secret', chat_id='456')
        def opener(req, timeout):
            self.assertEqual(json.loads(req.data), {'chat_id': '456', 'text': 'پیام'})
            self.assertEqual(timeout, 15)
            return io.BytesIO(b'{"ok":true}')
        self.assertTrue(majix.send_telegram(c, 'پیام', threading.Event(), opener))

    def test_error_hides_token(self):
        c = dict(majix.DEFAULTS, token='123:secret', chat_id='456')
        def opener(req, timeout):
            raise HTTPError(req.full_url, 401, 'bad', {}, None)
        with self.assertRaises(majix.TelegramError) as caught:
            majix.send_telegram(c, 'hello', threading.Event(), opener)
        self.assertNotIn('secret', str(caught.exception))

    def test_network_retry(self):
        calls = []
        class Stop:
            def is_set(self): return False
            def wait(self, delay): return False
        def opener(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise URLError('network')
            return io.BytesIO(b'{"ok":true}')
        c = dict(majix.DEFAULTS, token='123:secret', chat_id='456')
        self.assertTrue(majix.send_telegram(c, 'hello', Stop(), opener))
        self.assertEqual(len(calls), 3)

    def test_explicit_proxy_normalization(self):
        self.assertEqual(telegram_client.normalize_proxy('192.168.219.77:8080'),
                         'http://192.168.219.77:8080')
        self.assertEqual(telegram_client.normalize_proxy(''), '')
        for value in ('socks5://localhost:8080', 'http://localhost:99999',
                      'http://localhost', 'http://localhost:8080/path'):
            with self.assertRaises(ValueError):
                telegram_client.normalize_proxy(value)

    def test_proxy_is_used_for_https(self):
        with patch('telegram_client.request.build_opener') as build:
            telegram_client.make_opener({'proxy': '192.168.219.77:8080'})
        self.assertEqual(build.call_args.args[0].proxies,
                         {'http': 'http://192.168.219.77:8080',
                          'https': 'http://192.168.219.77:8080'})

    def test_telegram_validation_independent_of_scanner(self):
        c = dict(majix.DEFAULTS, token='123:abc', chat_id='456', count=0,
                 signal=[-100, -100])
        majix.validate(c, 'telegram')
        with self.assertRaises(ValueError):
            majix.validate(c)

    def test_scanner_validation_independent_of_telegram(self):
        c = dict(majix.DEFAULTS, token='', chat_id='', proxy='invalid')
        majix.validate(c, 'scan')

    def test_http_description_and_secret_redaction(self):
        c = dict(majix.DEFAULTS, token='123:secret', chat_id='456')
        def opener(req, timeout):
            body = json.dumps({'ok': False, 'error_code': 400,
                               'description': 'chat not found 123:secret'}).encode()
            raise HTTPError(req.full_url, 400, 'bad', {}, io.BytesIO(body))
        with self.assertRaises(majix.TelegramError) as caught:
            majix.send_telegram(c, 'hello', threading.Event(), opener)
        self.assertIn('chat not found', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_429_retry_after_is_honored(self):
        delays, calls = [], []
        class Stop:
            def is_set(self): return False
            def wait(self, delay):
                delays.append(delay)
                return False
        def opener(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                body = b'{"ok":false,"error_code":429,"parameters":{"retry_after":7}}'
                raise HTTPError(req.full_url, 429, 'rate limit', {}, io.BytesIO(body))
            return io.BytesIO(b'{"ok":true}')
        c = dict(majix.DEFAULTS, token='123:secret', chat_id='456')
        self.assertTrue(majix.send_telegram(c, 'hello', Stop(), opener))
        self.assertEqual(delays, [7])

    def test_focus_loss_pauses_and_resumes(self):
        calls, events = [], []
        class Stop:
            def is_set(self): return False
            def wait(self, delay): return False
        def action():
            calls.append(1)
            if len(calls) < 3:
                raise majix.FocusLost()
            return 'sample'
        self.assertEqual(majix.while_focused(action, Stop(), lambda *a: events.append(a)), 'sample')
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(events), 2)

    def test_scan_back_and_forth_and_failed_delivery(self):
        class Stop:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, delay): return self.stopped
        stop = Stop()
        positions, moves, sends = [], [], []
        class Desktop:
            idx = 0
            def __init__(self, c): pass
            def sample(self):
                positions.append(self.idx)
                if len(positions) == 6: stop.stopped = True
                return [(20, 180, 20), (200, 20, 20), (20, 180, 20)]
            def move(self, key, steps, event):
                moves.append(key)
                self.idx += 1 if key == 'down' else -1
        def sender(c, text, event):
            sends.append(text)
            if len(sends) == 1: raise majix.TelegramError('failed')
            return True
        c = dict(majix.DEFAULTS, count=3, sound=False)
        majix.scan(c, stop, lambda *args: None, Desktop, sender)
        self.assertEqual(positions, [0, 1, 2, 2, 1, 0])
        self.assertEqual(moves, ['down', 'down', 'up', 'up'])
        self.assertEqual(len(sends), 4)  # failed first row is retried, others deduplicated


if __name__ == '__main__':
    unittest.main()
