"""Start the exporter in an existing Chrome tab through user-approved DevTools access."""

import json
import os
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from websocket import WebSocketException, create_connection


class ChromeExportError(Exception):
    pass


def chrome_endpoint():
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise ChromeExportError('Выгрузка через открытую вкладку доступна в локальном Chrome на Windows.')
    port_file = Path(local) / 'Google' / 'Chrome' / 'User Data' / 'DevToolsActivePort'
    try:
        with port_file.open(encoding='utf-8') as handle:
            lines = handle.read(1024).splitlines()
        port, path = (line.strip() for line in lines[:2])
        if not port.isascii() or not port.isdecimal() or not 0 < int(port) < 65536:
            raise ValueError
        if not re.fullmatch(r'/devtools/browser(?:/[a-zA-Z0-9-]+)?', path):
            raise ValueError
    except (OSError, ValueError):
        raise ChromeExportError('В Chrome откройте chrome://inspect/#remote-debugging и включите Remote Debugging.') from None
    return f'ws://127.0.0.1:{port}{path}'


class ChromeExporter:
    def __init__(self, expression):
        self.expression = expression
        self.lock = Lock()
        self.socket = None
        self.endpoint = None
        self.sequence = 0

    def reset(self):
        socket = self.socket
        self.socket = self.endpoint = None
        if socket is not None:
            try:
                socket.close(timeout=0)
            except (OSError, WebSocketException):
                pass

    def connect(self):
        endpoint = chrome_endpoint()
        if self.socket is not None and endpoint == self.endpoint and self.socket.connected:
            return
        self.reset()
        self.socket = create_connection(endpoint, timeout=60, suppress_origin=True, http_no_proxy=['127.0.0.1'])
        self.endpoint = endpoint

    def command(self, method, params=None, session=None):
        self.sequence += 1
        command = {'id': self.sequence, 'method': method, 'params': params or {}}
        if session is not None:
            command['sessionId'] = session
        self.socket.send(json.dumps(command))
        deadline = monotonic() + 60
        while monotonic() < deadline:
            self.socket.settimeout(max(.1, deadline - monotonic()))
            result = json.loads(self.socket.recv())
            if result.get('id') != command['id']:
                continue
            if 'error' in result:
                raise ChromeExportError('Chrome отклонил команду запуска выгрузки.')
            return result.get('result', {})
        raise ChromeExportError('Chrome не ответил на запрос выгрузки.')

    def start(self):
        if not self.lock.acquire(blocking=False):
            raise ChromeExportError('Chrome уже обрабатывает запрос на выгрузку.')
        try:
            self.connect()
            targets = self.command('Target.getTargets').get('targetInfos', [])
            pages = []
            for target in targets:
                if target.get('type') != 'page':
                    continue
                url = urlsplit(target.get('url', ''))
                if (url.scheme, url.netloc) == ('https', 'chatgpt.com'):
                    pages.append(target)
            if not pages:
                raise ChromeExportError('В Chrome нет открытой вкладки chatgpt.com. Откройте вкладку со своим аккаунтом.')
            if len({page.get('browserContextId') for page in pages}) != 1:
                raise ChromeExportError('Chrome нашёл несколько сессий ChatGPT. Оставьте открытыми вкладки нужной сессии.')
            target = pages[0]['targetId']
            self.command('Target.activateTarget', {'targetId': target})
            attached = self.command('Target.attachToTarget', {'targetId': target, 'flatten': True})
            session = attached['sessionId']
            try:
                evaluated = self.command('Runtime.evaluate', {'expression': self.expression}, session)
                obj = evaluated.get('result', {}).get('objectId')
                if not obj or 'exceptionDetails' in evaluated:
                    raise ChromeExportError('Chrome не смог подготовить скрипт выгрузки.')
                result = self.command('Runtime.callFunctionOn', {
                    'objectId': obj, 'functionDeclaration': 'function () { this(); }', 'userGesture': True,
                }, session)
                if 'exceptionDetails' in result:
                    raise ChromeExportError('Chrome не смог запустить выгрузку.')
            finally:
                try:
                    self.command('Target.detachFromTarget', {'sessionId': session})
                except (ChromeExportError, OSError, ValueError, WebSocketException):
                    self.reset()
        except (OSError, ValueError, WebSocketException):
            self.reset()
            raise ChromeExportError('Не удалось подключиться к Chrome. Если браузер спрашивает разрешение, нажмите «Разрешить».') from None
        finally:
            self.lock.release()
