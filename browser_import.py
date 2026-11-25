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
from urllib.parse import quote, urlsplit
from uuid import UUID
import zipfile

from import_chatgpt import (
    API, MIB, Export, ImportFailure, RequestFailure, UnconfirmedWrite, WebUI,
    apply_import, convert,
)


ROOT = '/chatgpt-import/'
POST = ROOT + 'upload'
CHECK = POST + '/check'
EXPORT_SCRIPT = Path(__file__).with_name('convert.js')
GUIDE_GIF = Path(__file__).with_name('chatgpt-import.gif')
EXPORT_TARGET = '/* OPEN_WEBUI_IMPORT_PAGE */ null'
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
  <form id="upload" data-archive-limit="<!-- archive limit -->" hidden>
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
button { font: inherit; max-width: 100%; justify-self: start; margin-top: .5rem; padding: .55rem 1.05rem; cursor: pointer; }
button:disabled { cursor: wait; }
a { text-underline-offset: .2em; }
'''

JS = '''
const form = document.querySelector('#upload');
const status = document.querySelector('#status');
const button = form.querySelector('button');
const exportBookmark = document.querySelector('#export-bookmark');
const exportSection = document.querySelector('#export-bookmark-section');
let archives = [];
let completed = 0;
const totals = { counts: {}, warnings: 0, unlinked: 0, unavailable: new Set() };

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
      const batches = event.data.format === 3 ? event.data.archives
        : event.data.format === 2 ? [event.data.archive] : null;
      const accepted = Array.isArray(batches) && batches.length > 0
        && batches.every((archive) => archive instanceof Blob && archive.size > 0);
      if (accepted) {
        archives = batches;
        form.hidden = false;
        status.textContent = 'Данные из ChatGPT получены. Нажмите «Импортировать».';
        history.replaceState(null, '', location.pathname);
      } else {
        exportSection.hidden = false;
        status.textContent = 'Данные из ChatGPT не получены. Используйте закладку с этой страницы.';
      }
      window.removeEventListener('message', receive);
      sender.postMessage({type: 'chatgpt-export/received', transfer: transferId, accepted}, chatgptOrigin);
    };
    window.addEventListener('message', receive);
    sender.postMessage({type: 'chatgpt-export/ready', transfer: transferId,
      archiveLimit: Number(form.dataset.archiveLimit)}, chatgptOrigin);
  } else {
    exportSection.hidden = false;
    status.textContent = 'Браузер разорвал связь со вкладкой ChatGPT.';
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!archives.length || button.disabled) return;
  button.disabled = true;
  const total = completed + archives.length;
  try {
    while (archives.length) {
      const archive = archives[0];
      status.textContent = `Импортирую часть ${completed + 1} из ${total}. Оставьте страницу открытой.`;
      const check = await fetch('/chatgpt-import/upload/check', {
        method: 'POST', credentials: 'same-origin',
        headers: {'X-Archive-Size': String(archive.size)},
      });
      const response = check.ok ? await fetch('/chatgpt-import/upload', {
        method: 'POST', credentials: 'same-origin', body: archive,
        headers: {'Content-Type': 'application/zip'},
      }) : check;
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.import) {
        status.textContent = response.status === 413
          ? 'Часть данных не помещается в лимит сервиса. Обратитесь в поддержку.'
          : data?.error || 'Импорт прервался. Нажмите «Импортировать», чтобы продолжить.';
        return;
      }
      for (const [name, count] of Object.entries(data.import)) {
        totals.counts[name] = (totals.counts[name] || 0) + count;
      }
      totals.warnings += Object.values(data.warnings).reduce((sum, count) => sum + count, 0);
      totals.unlinked += data.files_unlinked;
      for (const section of data.personalization_unavailable) totals.unavailable.add(section);
      archives.shift();
      completed++;
    }
    const counts = totals.counts;
    status.textContent = `Добавлено чатов: ${counts.conversation_written || 0}. `
      + `Уже в истории: ${counts.conversation_unchanged || 0}. `
      + `Пропущено изменённых бесед: ${counts.conversation_changed || 0}. `
      + `Записей памяти добавлено: ${counts.memory_written || 0}. Уже в памяти: ${counts.memory_unchanged || 0}. `
      + `Файлов без связи с сообщениями: ${totals.unlinked}. Предупреждений: ${totals.warnings}.`;
    if (totals.unavailable.size) {
      status.textContent += ` ChatGPT не вернул следующие разделы: ${[...totals.unavailable].join(', ')}.`;
    }
    form.hidden = true;
  } catch {
    status.textContent = 'Связь с сервисом прервалась. Нажмите «Импортировать», чтобы продолжить.';
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
        if self.path == ROOT:
            page = HTML.replace('<!-- exporter bookmark -->', escape(self.server.export_bookmarklet, quote=True))
            page = page.replace('<!-- archive limit -->', str(self.server.options.archive_limit_mib * MIB))
            self.reply(200, 'text/html; charset=utf-8', page.encode('utf-8'))
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

    def archive_size(self, header):
        sizes = self.headers.get_all(header, [])
        if len(sizes) != 1 or not sizes[0].isascii() or not sizes[0].isdigit() or len(sizes[0]) > 12:
            raise UploadError(411, 'Сервису не передан размер ZIP.')
        size = int(sizes[0])
        limit = self.server.options.archive_limit_mib
        if not size:
            raise UploadError(400, 'Получен пустой ZIP.')
        if size > limit * MIB:
            raise UploadError(413, 'Часть данных не помещается в лимит сервиса.')
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
        with BytesIO() as archive:
            self.receive(archive, size)
            warnings = Counter()
            with zipfile.ZipFile(archive) as zip_archive:
                export = Export(zip_archive, settings.max_expanded_mib * MIB)
                conversations, project_rows, assets = export.inventory()
                if not conversations:
                    raise UploadError(400, 'В ZIP не найдено бесед ChatGPT.')
                chats, projects = convert(conversations, project_rows, assets, warnings)
                return {
                    **apply_import(
                        api, user, export, chats, projects, assets,
                        upload_limit=settings.upload_limit_mib * MIB,
                        journal_path=settings.data_dir / f"{user['id']}.sqlite3",
                    ),
                    'warnings': dict(warnings),
                    'personalization_unavailable': export.personalization_unavailable,
                }

    def do_POST(self):
        self.close_connection = True
        if self.path not in {POST, CHECK}:
            self.json(404, {'error': 'Страница не найдена.'})
            return
        try:
            if self.headers.get('Origin') != self.server.options.public_origin:
                raise UploadError(403, 'Загрузите архив со страницы импорта Open WebUI.')
            if self.headers.get('Transfer-Encoding'):
                raise UploadError(411, 'Прокси должен передавать размер загрузки.')
            checking = self.path == CHECK
            if checking:
                if self.headers.get_all('Content-Length', []) not in ([], ['0']):
                    raise UploadError(400, 'Запрос проверки должен быть без тела.')
                self.archive_size('X-Archive-Size')
            else:
                if self.headers.get_content_type() != 'application/zip':
                    raise UploadError(400, 'Ожидался ZIP из вкладки ChatGPT.')
                size = self.archive_size('Content-Length')
            api, user = self.authenticated_user()
            if not self.server.slots.acquire(blocking=False):
                raise UploadError(503, 'Сейчас выполняется другой импорт. Повторите загрузку позже.')
            try:
                result = {} if checking else self.import_upload(api, user, size)
            finally:
                self.server.slots.release()
        except UploadError as error:
            print(f'Import request rejected: HTTP {error.status}', file=sys.stderr, flush=True)
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
        except MemoryError:
            self.json(503, {'error': 'Сервису не хватило оперативной памяти для импорта ZIP.'})
        except (OSError, sqlite3.Error):
            self.json(503, {'error': 'Сервер не завершил импорт. Можно повторить отправку того же ZIP.'})
        else:
            self.json(200, result)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True, help='Trusted Open WebUI URL')
    parser.add_argument('--public-origin', required=True, help='Browser origin, for example https://webui.example')
    parser.add_argument('--local-webui', help='Open WebUI origin for direct local access on a different port')
    parser.add_argument('--port', default=3012, type=positive_int, help='Loopback listener port')
    parser.add_argument('--data-dir', required=True, type=Path, help='Private directory for the import receipts')
    parser.add_argument('--upload-limit-mib', required=True, type=positive_int)
    parser.add_argument('--archive-limit-mib', type=positive_int, help='Maximum incoming ZIP size in MiB')
    parser.add_argument('--max-expanded-mib', default=2048, type=positive_int)
    parser.add_argument('--timeout', default=120, type=positive_int, help='Open WebUI timeout in seconds')
    args = parser.parse_args()
    args.archive_limit_mib = args.archive_limit_mib or args.upload_limit_mib
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
        with ThreadingHTTPServer(('127.0.0.1', args.port), BrowserImport) as server:
            server.options = args
            server.slots = BoundedSemaphore(2)
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
