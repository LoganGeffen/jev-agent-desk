import functools
import http.server
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request

from server import make_server

from playwright.sync_api import sync_playwright


class WakeBrowserTests(unittest.TestCase):
    def test_real_local_detector_drives_voice_capture_without_idle_cloud_audio(self):
        root = Path(__file__).parent
        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/wake-test':
                    body = b'<html><body><script src="/readback.js"></script><script src="/wake.js"></script><script src="/voice.js"></script></body></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                requests, errors = [], []
                page.on('request', lambda req: requests.append(req.url))
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                page.add_init_script('''
                  const original = document.getElementById.bind(document);
                  document.getElementById = id => {
                    let element = original(id);
                    if (!element) {element = document.createElement('div'); element.id = id; document.documentElement.append(element);}
                    return element;
                  };
                  window.cloudSockets = []; window.commands = []; window.permissions = 0;
                  window.AudioContext = class {
                    sampleRate = 16000; destination = {}; currentTime = 0;
                    audioWorklet = {addModule: async () => {}};
                    async resume() {} async close() {this.closed = true;}
                    createMediaStreamSource() {return {connect() {}, disconnect() {}};}
                  };
                  window.AudioWorkletNode = class {port = {}; connect() {} disconnect() {}};
                  navigator.mediaDevices.getUserMedia = async () => {
                    permissions++; return {getTracks: () => [{stop() {}}]};
                  };
                  window.WebSocket = class {
                    static OPEN = 1; readyState = 1; bufferedAmount = 0; messages = [];
                    constructor(url) {this.url = url; cloudSockets.push(this);}
                    send(message) {this.messages.push(JSON.parse(message));}
                    close() {this.closed = true;}
                  };
                ''')
                page.goto(origin + '/wake-test')
                page.evaluate('''async () => {
                  window.voice = new VoiceControls(async text => commands.push(text));
                  voice.configure({configured:true, websocket_url:'ws://synthetic-request-only'});
                  voice.localWake = true;
                  await voice.start();
                  window.feed = async name => {
                    const pcm = new Int16Array(await (await fetch('/test-fixtures/wake/'+name+'.pcm')).arrayBuffer());
                    const samples = new Int16Array(pcm.length + 48000); samples.set(pcm,16000);
                    for(let i=0; i<samples.length; i+=2048)
                      voice.capture.processor.port.onmessage({data:{pcm:samples.slice(i,i+2048),rms:.1}});
                  };
                }''')
                page.wait_for_timeout(2100)
                for name in ('negative', 'confusing'):
                    page.evaluate('feed', name)
                    self.assertEqual(page.evaluate('cloudSockets.length'), 0)
                    self.assertEqual(page.evaluate('voice.draft'), None)
                page.evaluate("feed('command')")
                self.assertEqual(page.evaluate('cloudSockets.length'), 1)
                self.assertEqual(page.evaluate('cloudSockets[0].messages.length'), 0)
                result = page.evaluate('''async () => {
                  const socket = cloudSockets[0];
                  const receive = (type,text) => socket.onmessage({data:JSON.stringify({message_type:type,text})});
                  receive('session_started');
                  const buffered = socket.messages.length;
                  receive('committed_transcript','Earlier background command.');
                  const ignored = voice.draft === null;
                  receive('committed_transcript','Earlier words. Hey Jarvis, read Dog Data.');
                  await voice.finishDraft(true);
                  return {buffered, ignored, commands, closed:socket.closed, ready:voice.capture.ready,
                    waiting:voice.capture.wakeState, permissions};
                }''')
                self.assertTrue(result['ignored'])
                self.assertGreater(result['buffered'], 0)
                self.assertEqual(result['commands'], ['read Dog Data.'])
                self.assertFalse(result.get('closed', False))
                self.assertTrue(result['ready'])
                self.assertEqual(result['waiting'], 'listening')
                self.assertEqual(result['permissions'], 1)
                page.evaluate("cloudSockets[0].onmessage({data:JSON.stringify({message_type:'committed_transcript',text:'Explain those numbers.'})});")
                page.evaluate('voice.finishDraft(true)')
                self.assertEqual(page.evaluate('commands'), ['read Dog Data.', 'Explain those numbers.'])
                self.assertEqual(page.evaluate('cloudSockets.length'),1)
                page.evaluate('voice.wakeIdleMs=80;voice.wakeActivity(voice.capture)')
                page.wait_for_function("voice.capture.wakeState === 'waiting'")
                self.assertTrue(page.evaluate('cloudSockets[0].closed'))
                page.evaluate('voice.wakeIdleMs=60000')
                page.wait_for_timeout(2100)
                page.evaluate("feed('positive')")
                self.assertEqual(page.evaluate('cloudSockets.length'), 2)
                self.assertEqual(page.evaluate('permissions'), 1)
                page.evaluate("cloudSockets[1].onmessage({data:JSON.stringify({message_type:'session_started'})}); cloudSockets[1].onmessage({data:JSON.stringify({message_type:'committed_transcript',text:'Hey Jarvis.'})});")
                self.assertEqual(page.evaluate('commands'), ['read Dog Data.', 'Explain those numbers.'])
                self.assertEqual(errors, [])
                self.assertTrue(all(url.startswith(origin) for url in requests))
                self.assertTrue(any(url.endswith('.wasm') for url in requests))
                page.evaluate('voice.stop()')
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


    def test_production_server_serves_exact_local_wake_assets_and_rejects_traversal(self):
        server = make_server(None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        root = Path(__file__).parent
        try:
            assets = [root / 'wake.js', *[p for p in (root / 'wake-assets').rglob('*') if p.is_file()]]
            types = {'.js': 'text/javascript', '.wasm': 'application/wasm', '.json': 'application/json'}
            for path in assets:
                with self.subTest(path=str(path.relative_to(root))):
                    with urllib.request.urlopen(origin + '/' + str(path.relative_to(root))) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers['Content-Type'], types.get(path.suffix, 'application/octet-stream'))
                        self.assertEqual(response.read(), path.read_bytes())
            for path in ('/wake-assets/missing.wasm', '/wake-assets/../server.py',
                         '/wake-assets/%2e%2e/server.py', '/wake-assets/wasm/../../server.py'):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(origin + path)
                self.assertEqual(raised.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
