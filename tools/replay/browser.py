"""Small Chrome DevTools pipe client (standard library, no network listener)."""
import base64
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time


class Browser:
    def __enter__(self):
        self.profile = tempfile.TemporaryDirectory(prefix='chrome-', dir=Path(__file__).parent)
        self.log = open(Path(self.profile.name) / 'chrome.log', 'wb')
        incoming, self.writer = os.pipe()
        self.reader, outgoing = os.pipe()

        def setup():
            os.dup2(incoming, 3)
            os.dup2(outgoing, 4)
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)

        chrome = next((shutil.which(c) for c in ['google-chrome', 'chromium', 'chromium-browser'] if shutil.which(c)), None)
        if not chrome:
            raise RuntimeError('需要 Google Chrome 或 Chromium')
        self.process = subprocess.Popen([chrome, '--headless', '--no-sandbox', '--disable-gpu',
            '--disable-dev-shm-usage', '--disable-background-networking', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-pipe', '--allow-file-access-from-files',
            '--autoplay-policy=no-user-gesture-required', f'--user-data-dir={self.profile.name}', 'about:blank'],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, close_fds=False, preexec_fn=setup)
        os.close(incoming)
        os.close(outgoing)
        self.counter, self.buffer, self.session = 0, b'', None
        try:
            targets = self.call('Target.getTargets')['targetInfos']
            target = next(t for t in targets if t['type'] == 'page')
            self.session = self.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
            self.call('Page.enable')
            self.call('Runtime.enable')
            self.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 720, 'deviceScaleFactor': 1, 'mobile': False})
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def call(self, method, params=None):
        self.counter += 1
        request = {'id': self.counter, 'method': method, 'params': params or {}}
        if self.session:
            request['sessionId'] = self.session
        os.write(self.writer, json.dumps(request).encode() + b'\0')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b'\0' in self.buffer:
                raw, self.buffer = self.buffer.split(b'\0', 1)
                result = json.loads(raw)
                if result.get('id') == self.counter:
                    if 'error' in result:
                        raise RuntimeError(result['error'])
                    return result.get('result', {})
            if select.select([self.reader], [], [], 1)[0]:
                chunk = os.read(self.reader, 1024 * 1024)
                if not chunk:
                    raise RuntimeError('Chrome 调试管道关闭')
                self.buffer += chunk
        raise TimeoutError(f'Chrome 超时：{method}')

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True})
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'])
        return result.get('result', {}).get('value')

    def open(self, path):
        self.call('Page.navigate', {'url': Path(path).resolve().as_uri()})
        for _ in range(100):
            if self.evaluate('!!window.replay'):
                self.evaluate('document.fonts.ready.then(() => true)')
                return
            time.sleep(.05)
        raise TimeoutError('回放页面未初始化')

    def screenshot(self, path):
        self.evaluate('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
        result = self.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
        Path(path).write_bytes(base64.b64decode(result['data']))

    def __exit__(self, *args):
        # Let Chrome stop its profile-writing children before removing the profile.
        if self.process.poll() is None:
            try:
                self.call('Browser.close')
            except (OSError, RuntimeError, TimeoutError):
                self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        os.close(self.reader)
        os.close(self.writer)
        self.log.close()
        self.profile.cleanup()
