#!/usr/bin/env python3
"""Serve a browser upload page alongside Open WebUI. Python 3.10+."""

import argparse
from collections import Counter
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import socket
import sqlite3
import sys
from threading import BoundedSemaphore
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from uuid import UUID
import zipfile

from chrome_export import ChromeExportError, ChromeExporter
from import_chatgpt import (
    API, MIB, Export, ImportFailure, RequestFailure, UnconfirmedWrite, WebUI, apply_import,
    convert, positive_int, summarize,
)


ROOT = '/chatgpt-import/'
POST = ROOT + 'upload'
START_EXPORT = ROOT + 'start-export'
EXPORT_SCRIPT = Path(__file__).with_name('convert.js')
GUIDE_GIF = Path(__file__).with_name('chatgpt-import.gif')
EXPORT_TARGET = '/* OPEN_WEBUI_IMPORT_PAGE */ null'
LOCAL_EXPORT = '''
  <form id="export" aria-label="Выгрузка из текущей вкладки Chrome">
    <button type="submit">Выгрузить из открытого ChatGPT</button>
    <p id="export-status" role="status" aria-live="polite"></p>
  </form>
'''
HTML = '''<!doctype html>
<html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Импорт ChatGPT</title>
<link rel="stylesheet" href="/chatgpt-import/style.css">
<script defer src="/chatgpt-import/import.js"></script>
<main>
  <a href="/">← Open WebUI</a>
  <h1>Импорт ChatGPT</h1>
  <section id="export-bookmark-section" aria-label="Выгрузка из ChatGPT">
    <a id="export-bookmark" href="<!-- exporter bookmark -->" draggable="true">Выгрузить чаты в Open WebUI</a>
    <p>Если панели закладок под адресной строкой нет, нажмите <kbd>Ctrl+Shift+B</kbd>. Один раз перетащите синюю ссылку на эту панель. Откройте <a href="https://chatgpt.com/">ChatGPT</a> и нажмите сохранённую закладку.</p>
    <img class="import-guide" src="/chatgpt-import/chatgpt-import.gif" width="960" height="720" alt="Демонстрация переноса чатов из ChatGPT в Open WebUI">
  </section>
  <!-- local Chrome export -->
  <form id="upload" hidden>
    <button type="submit">Импортировать</button>
  </form>
  <p id="status" role="status" aria-live="polite"></p>
</main></html>'''

CSS = '''
:root { color-scheme: light dark; font: 17px/1.55 system-ui, sans-serif; }
body { margin: 0; }
main { width: min(34rem, calc(100% - 2.5rem)); margin: clamp(2rem, 10vh, 6rem) auto; }
h1 { margin: 1.75rem 0 .5rem; line-height: 1.15; font-size: clamp(2rem, 7vw, 3rem); }
p { margin: 0 0 1.4rem; }
form { display: grid; gap: .6rem; margin: 1.6rem 0 1rem; }
form[hidden] { display: none; }
#export-bookmark { display: inline-block; margin: .35rem 0 .6rem; padding: .55rem .85rem; border: 1px solid #60a5fa; border-radius: .5rem; background: #1d4ed8; color: #fff; text-decoration: none; cursor: grab; }
#export-bookmark:focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
.import-guide { display: block; max-width: 100%; height: auto; }
button { font: inherit; max-width: 100%; }
button { justify-self: start; margin-top: .5rem; padding: .55rem 1.05rem; cursor: pointer; }
button:disabled { cursor: wait; }
a { text-underline-offset: .2em; }
'''

JS = '''
const form = document.querySelector('#upload');
const status = document.querySelector('#status');
const button = form.querySelector('button');
const exportForm = document.querySelector('#export');
const exportBookmark = document.querySelector('#export-bookmark');
const exportSection = document.querySelector('#export-bookmark-section');
let transferredArchive = null;

exportBookmark.addEventListener('click', (event) => {
  event.preventDefault();
  status.textContent = 'Если панели закладок нет, нажмите Ctrl+Shift+B. Перетащите туда синюю ссылку, затем нажмите закладку во вкладке ChatGPT.';
});

const transferId = new URLSearchParams(location.hash.slice(1)).get('transfer');
const chatgptOrigin = 'https://chatgpt.com';
if (transferId && /^[a-f0-9-]{36}$/.test(transferId)) {
  exportSection.hidden = true;
  const sender = window.opener;
  if (sender) {
    status.textContent = 'Ожидаю архив из вкладки ChatGPT…';
    const receive = (event) => {
      if (event.origin !== chatgptOrigin || event.source !== sender || event.data?.transfer !== transferId) return;
      if (event.data.type === 'chatgpt-export/cancel') {
        window.removeEventListener('message', receive);
        exportSection.hidden = false;
        status.textContent = 'ChatGPT не передал архив.';
        return;
      }
      if (event.data.type !== 'chatgpt-export/archive') return;
      const archive = event.data.archive;
      const currentFormat = event.data.format === 2;
      const accepted = currentFormat && archive instanceof Blob && archive.size > 0;
      if (accepted) {
        transferredArchive = archive;
        form.hidden = false;
        status.textContent = 'Архив из ChatGPT получен. Нажмите «Импортировать».';
        history.replaceState(null, '', location.pathname);
      } else {
        exportSection.hidden = false;
        status.textContent = currentFormat ? 'Из ChatGPT пришёл пустой или недопустимый файл.'
          : 'Сработала старая закладка. Перетащите новую ссылку на панель избранного и запустите её во вкладке ChatGPT.';
      }
      window.removeEventListener('message', receive);
      sender.postMessage({type: 'chatgpt-export/received', transfer: transferId, accepted}, chatgptOrigin);
    };
    window.addEventListener('message', receive);
    sender.postMessage({type: 'chatgpt-export/ready', transfer: transferId}, chatgptOrigin);
  } else {
    exportSection.hidden = false;
    status.textContent = 'Браузер разорвал связь со вкладкой ChatGPT.';
  }
}

if (exportForm) {
  const exportStatus = document.querySelector('#export-status');
  const exportButton = exportForm.querySelector('button');
  exportForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    exportButton.disabled = true;
    exportStatus.textContent = 'Подключаюсь к открытой вкладке Chrome…';
    try {
      const response = await fetch('/chatgpt-import/start-export', {
        method: 'POST', credentials: 'same-origin',
      });
      const data = await response.json();
      if (!response.ok) {
        exportStatus.textContent = data.error || 'Не удалось запустить выгрузку.';
        return;
      }
      exportStatus.textContent = 'Выгрузка запущена. ZIP поступит во вкладку импорта.';
    } catch {
      exportStatus.textContent = 'Не удалось запустить выгрузку.';
    } finally {
      exportButton.disabled = false;
    }
  });
  if (transferId) exportForm.hidden = true;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!transferredArchive) return;
  button.disabled = true;
  status.textContent = 'Импортирую чаты. Оставьте страницу открытой.';
  try {
    const response = await fetch('/chatgpt-import/upload', {
      method: 'POST', credentials: 'same-origin', body: transferredArchive,
      headers: {'Content-Type': 'application/zip'},
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      status.textContent = data?.error || (response.status === 413
        ? 'Архив превышает лимит загрузки.'
        : 'Сервер не завершил импорт. Можно повторить отправку того же ZIP.');
      return;
    }
    const counts = data.import;
    const warnings = Object.values(data.warnings).reduce((sum, count) => sum + count, 0);
    status.textContent = `Добавлено чатов: ${counts.conversation_written || 0}. `
      + `Уже в истории: ${counts.conversation_unchanged || 0}. `
      + `Пропущено изменённых бесед: ${counts.conversation_changed || 0}. `
      + `Записей памяти добавлено: ${counts.memory_written || 0}. Уже в памяти: ${counts.memory_unchanged || 0}. `
      + `Файлов без связи с сообщениями: ${data.files_unlinked}. Предупреждений: ${warnings}.`;
    if (data.personalization_unavailable?.length) {
      status.textContent += ` ChatGPT не вернул следующие разделы: ${data.personalization_unavailable.join(', ')}.`;
    }
    transferredArchive = null;
    form.hidden = true;
  } catch {
    status.textContent = 'Связь прервалась. Можно повторить отправку того же ZIP.';
  } finally {
    button.disabled = false;
  }
});
'''

ASSETS = {
    ROOT + 'style.css': ('text/css; charset=utf-8', CSS),
    ROOT + 'import.js': ('text/javascript; charset=utf-8', JS),
}


class UploadError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class BrowserImport(BaseHTTPRequestHandler):
    server_version = 'ChatGPTImport'

    def setup(self):
        super().setup()
        self.connection.settimeout(90)

    def log_message(self, format, *args):
        # Request paths, cookies, source filenames and chat content stay out of access logs.
        return

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def json(self, status, payload):
        self.reply(status, 'application/json; charset=utf-8', json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        local_webui = self.server.options.local_webui
        if local_webui and self.path == '/':
            self.send_response(303)
            self.send_header('Location', local_webui + '/')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path == ROOT or (self.path == START_EXPORT and self.server.chrome):
            page = HTML.replace('<!-- local Chrome export -->', LOCAL_EXPORT if self.path == START_EXPORT else '')
            page = page.replace('<!-- exporter bookmark -->', escape(self.server.export_bookmarklet, quote=True))
            self.reply(200, 'text/html; charset=utf-8', page.encode('utf-8'))
            return
        if self.path == ROOT + 'convert.js':
            self.reply(200, 'text/javascript; charset=utf-8', self.server.export_source.encode('utf-8'))
            return
        if self.path == ROOT + GUIDE_GIF.name:
            try:
                body = GUIDE_GIF.read_bytes()
            except FileNotFoundError:
                self.json(404, {'error': 'GIF инструкции не найден.'})
            else:
                self.reply(200, 'image/gif', body)
            return
        if self.path not in ASSETS:
            self.json(404, {'error': 'Страница не найдена.'})
            return
        content_type, content = ASSETS[self.path]
        self.reply(200, content_type, content.encode('utf-8'))

    def authenticated_user(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
        except CookieError:
            cookie = {}
        token = cookie.get('token')
        if token is None or not token.value:
            raise UploadError(401, 'Войдите в Open WebUI и вернитесь на эту страницу.')
        api = WebUI(self.server.options.upstream, token.value, self.server.options.timeout)
        try:
            user = api.get(API + '/auths/')
        except RequestFailure as error:
            if error.status in {401, 403}:
                raise UploadError(401, 'Сессия истекла. Войдите в Open WebUI и повторите импорт.') from None
            raise
        try:
            user_id = str(UUID(user['id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise UploadError(401, 'Open WebUI не подтвердил вашу учётную запись.') from None
        return api, {'id': user_id, 'role': user.get('role')}

    def request_file(self):
        if self.headers.get('Origin') != self.server.options.public_origin:
            raise UploadError(403, 'Загрузите архив со страницы импорта Open WebUI.')
        if self.headers.get_content_type() != 'application/zip':
            raise UploadError(400, 'Ожидался ZIP из вкладки ChatGPT.')
        sizes = self.headers.get_all('Content-Length', [])
        if len(sizes) != 1 or self.headers.get('Transfer-Encoding') or not sizes[0].isascii() or not sizes[0].isdigit():
            raise UploadError(411, 'Прокси должен передавать размер загрузки.')
        if len(sizes[0]) > 12:
            raise UploadError(413, 'Архив превышает лимит загрузки.')
        size = int(sizes[0])
        if size <= 0 or size > self.server.options.upload_limit_mib * MIB:
            raise UploadError(413, 'Архив пуст или превышает лимит загрузки.')
        return size

    def receive(self, buffer, length):
        remaining = length
        while remaining:
            block = self.rfile.read(min(MIB, remaining))
            if not block:
                raise UploadError(400, 'Загрузка прервалась. Отправьте тот же ZIP ещё раз.')
            buffer.write(block)
            remaining -= len(block)
        buffer.seek(0)

    def import_upload(self, api, user, size):
        settings = self.server.options
        max_zip = settings.upload_limit_mib * MIB
        args = SimpleNamespace(
            user_id=user['id'], upload_limit_mib=settings.upload_limit_mib, retry_unconfirmed=False,
            journal=settings.data_dir / f"{user['id']}.sqlite3",
        )
        with BytesIO() as archive:
            self.receive(archive, size)
            warnings = Counter()
            source = Export(None, max_zip, settings.max_expanded_mib * MIB)
            with source.open_zip(archive, size) as export:
                conversations, project_rows, assets = export.inventory()
                if not conversations:
                    raise UploadError(400, 'В ZIP не найдено бесед ChatGPT.')
                chats, projects = convert(conversations, project_rows, assets, warnings)
                summary = summarize(conversations, chats, projects, assets, warnings)
                return {
                    **summary,
                    **apply_import(args, export, chats, projects, assets, api=api, user=user),
                    'personalization_unavailable': export.personalization_unavailable,
                }

    def do_POST(self):
        self.close_connection = True
        if self.path == START_EXPORT:
            self.start_chrome_export()
            return
        if self.path != POST:
            self.json(404, {'error': 'Страница не найдена.'})
            return
        try:
            size = self.request_file()
            api, user = self.authenticated_user()
            if not self.server.slots.acquire(blocking=False):
                raise UploadError(503, 'Сейчас выполняется другой импорт. Повторите загрузку позже.')
            try:
                result = self.import_upload(api, user, size)
            finally:
                self.server.slots.release()
        except UploadError as error:
            self.json(error.status, {'error': error.message})
        except RequestFailure as error:
            if error.status in {401, 403}:
                self.json(403, {'error': 'Open WebUI отклонил запрос. Проверьте права на импорт для своей учётной записи.'})
            elif error.status == 413:
                self.json(413, {'error': 'Размер файла превышает лимит Open WebUI.'})
            else:
                self.json(502, {'error': 'Open WebUI прервал импорт. Можно повторить отправку того же ZIP.'})
        except UnconfirmedWrite:
            self.json(409, {'error': 'Open WebUI ещё не подтвердил одну из записей. Повторите загрузку позже. '
                                    'Если сообщение сохранится, обратитесь в поддержку.'})
        except ImportFailure as error:
            self.json(409, {'error': f'Импорт остановлен. {error}'})
        except (zipfile.BadZipFile, ValueError, TypeError, KeyError, AttributeError, RuntimeError, EOFError):
            self.json(400, {'error': 'Не удалось прочитать ZIP или ответ Open WebUI.'})
        except (OSError, sqlite3.Error):
            self.json(503, {'error': 'Сервер не завершил импорт. Можно повторить отправку того же ZIP.'})
        else:
            self.json(200, result)

    def start_chrome_export(self):
        if self.server.chrome is None:
            self.json(404, {'error': 'Запуск через Chrome доступен в локальной установке.'})
            return
        options = self.server.options
        if (self.headers.get('Origin') != options.public_origin
                or self.headers.get('Host') != urlsplit(options.public_origin).netloc):
            self.json(403, {'error': 'Запустите выгрузку со страницы импорта Open WebUI.'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if lengths not in ([], ['0']) or self.headers.get('Transfer-Encoding'):
            self.json(400, {'error': 'Запрос запуска выгрузки должен быть пустым.'})
            return
        try:
            self.authenticated_user()
            self.server.chrome.start()
        except UploadError as error:
            self.json(error.status, {'error': error.message})
        except ChromeExportError as error:
            self.json(409, {'error': str(error)})
        except (RequestFailure, OSError, ValueError):
            self.json(503, {'error': 'Не удалось запустить выгрузку. Проверьте сессию Open WebUI и Chrome.'})
        else:
            self.json(202, {'started': True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True, help='Trusted Open WebUI URL')
    parser.add_argument('--public-origin', required=True, help='Browser origin, for example https://webui.example')
    parser.add_argument('--local-webui', help='Open WebUI origin for direct local access on a different port')
    parser.add_argument('--port', default=3012, type=positive_int, help='Loopback listener port')
    parser.add_argument('--data-dir', required=True, type=Path, help='Private directory for the import receipts')
    parser.add_argument('--upload-limit-mib', required=True, type=positive_int)
    parser.add_argument('--max-expanded-mib', default=2048, type=positive_int)
    parser.add_argument('--timeout', default=120, type=positive_int, help='Open WebUI timeout in seconds')
    args = parser.parse_args()
    origin = urlsplit(args.public_origin)
    if (origin.scheme not in {'http', 'https'} or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment
            or (origin.scheme == 'http' and origin.hostname not in {'127.0.0.1', 'localhost', '::1'})):
        parser.error('--public-origin requires an HTTPS origin or HTTP loopback, without a trailing slash')
    if args.port > 65_535:
        parser.error('--port must be at most 65535')
    if args.local_webui:
        local = urlsplit(args.local_webui)
        if (origin.scheme != 'http' or local.scheme != 'http' or local.hostname != origin.hostname
                or local.username or local.password or local.path or local.query or local.fragment):
            parser.error('--local-webui requires the same HTTP loopback hostname and no trailing slash')
    try:
        WebUI(args.upstream, '', args.timeout)
        args.data_dir = args.data_dir.resolve()
        args.data_dir.mkdir(mode=0o700, exist_ok=True, parents=True)
        export_script = EXPORT_SCRIPT.read_text(encoding='utf-8-sig')
        if export_script.count(EXPORT_TARGET) != 1:
            raise ImportFailure('Missing export script target')
        target = json.dumps(args.public_origin + ROOT)
        export_source = export_script.replace(EXPORT_TARGET, target, 1)
        export_bookmarklet = 'javascript:' + quote('void (() => {\n' + export_source + '\n})()', safe='')
        chrome = None
        if args.local_webui:
            chrome = ChromeExporter('(() => {\n' + export_source + '\n})')
        with ThreadingHTTPServer(('127.0.0.1', args.port), BrowserImport) as server:
            server.options = args
            server.slots = BoundedSemaphore(2)
            server.chrome = chrome
            server.export_source = export_source
            server.export_bookmarklet = export_bookmarklet
            print(f'Browser importer listening on 127.0.0.1:{args.port}', flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ImportFailure, OSError) as error:
        print(f'Cannot start browser importer: {type(error).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
