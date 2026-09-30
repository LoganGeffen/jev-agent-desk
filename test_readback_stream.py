import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from spoken_reply import ReadbackCache
from server import make_server
import test_playground


class ReadbackStreamTests(unittest.TestCase):
    def test_cache_shares_inflight_text_and_never_substitutes_another_reply(self):
        first = threading.Event()
        finish = threading.Event()
        chunks = []
        def render(text, mode, emit):
            emit('First. ')
            first.set()
            finish.wait(3)
            emit(text)
            return {'text': 'First. ' + text, 'mode': 'rendition'}
        cache = ReadbackCache()
        self.addCleanup(cache.pool.shutdown)
        with patch('spoken_reply.render_spoken_reply', side_effect=render) as generate:
            cache.prepare('Original')
            self.assertTrue(first.wait(2))
            worker = threading.Thread(target=lambda: cache.render('Original', on_text=chunks.append))
            worker.start()
            for _ in range(100):
                if chunks: break
                threading.Event().wait(.01)
            self.assertEqual(chunks, ['First. '])
            finish.set(); worker.join(3)
            self.assertEqual(cache.render('Original')['text'], 'First. Original')
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(cache.render('Different')['text'], 'First. Different')
            self.assertEqual(generate.call_count, 2)

    def test_http_flushes_first_text_before_generation_finishes(self):
        fixture = test_playground.PlaygroundTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        server = make_server(fixture.app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.addCleanup(fixture.app.readbacks.pool.shutdown)
        finish = threading.Event()
        def render(text, mode, emit):
            emit('First sentence. ')
            finish.wait(3)
            return {'text': 'First sentence.', 'mode': 'rendition'}
        request = Request(f'http://127.0.0.1:{server.server_port}/api/spoken-version',
                          data=json.dumps({'text': 'Original', 'stream': True}).encode(),
                          headers={'Content-Type': 'application/json'})
        with patch('spoken_reply.render_spoken_reply', render), urlopen(request, timeout=5) as response:
            self.assertEqual(json.loads(response.readline()), {'type': 'text', 'text': 'First sentence. '})
            finish.set()
            self.assertEqual(json.loads(response.readline())['result']['text'], 'First sentence.')
