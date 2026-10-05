import functools
import http.server
from pathlib import Path
import threading
import unittest

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
                  document.getElementById('wake-mode').checked = true;
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
                self.assertTrue(result['closed'])
                self.assertTrue(result['ready'])
                self.assertEqual(result['waiting'], 'waiting')
                self.assertEqual(result['permissions'], 1)
                self.assertEqual(errors, [])
                self.assertTrue(all(url.startswith(origin) for url in requests))
                self.assertTrue(any(url.endswith('.wasm') for url in requests))
                page.evaluate('voice.stop()')
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
