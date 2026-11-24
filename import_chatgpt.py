#!/usr/bin/env python3
"""Pack and import a ChatGPT export through the Open WebUI API. Python 3.10+."""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
import getpass
import hashlib
from http.client import HTTPException as TransportError
from io import BytesIO
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import zipfile

import pyzipper
from Cryptodome.Cipher import AES


MIB = 1024 * 1024
PASSPHRASE_LIMIT = 1024
ENCRYPTED_PAYLOAD = 'export.zip'
BROWSER_ENCRYPTED_PAYLOAD = 'export.gcm'
BROWSER_MAGIC = b'CGZIPGCM1'
BROWSER_SALT_LENGTH = 16
BROWSER_NONCE_LENGTH = 12
BROWSER_TAG_LENGTH = 16
BROWSER_KDF_ROUNDS = 600_000
MARKER = 'chatgpt_import'
API = '/api/v1'
NOT_FOUND = "We could not find what you're looking for :/"
SOURCE_JSON = re.compile(r'(conversations|projects)(?:[_.-]?\d+)?\.json', re.I)
LOCAL_LINK = re.compile(r'(?<=\()<?((?:sandbox:/mnt/data/|attachment:)[^)>\n]+)>?(?=\))')
MARKDOWN_CODE = re.compile(
    r'^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n[\s\S]*?'
    r'(?:^[ \t]{0,3}(?P=fence)[`~]*[ \t]*(?=\n|\Z)|\Z)'
    r'|(?<!`)(?P<ticks>`+)(?!`)[^\n]*?(?<!`)(?P=ticks)(?!`)', re.M,
)
CHATGPT_TAG = re.compile(
    r'<(?P<closing>/)?(?P<tag>box|row|text|badge|divider|favicon|AsyncImage|Cite|Link|FileCite)\b'
    r'''(?P<attrs>(?:[^<>"']|"[^"]*"|'[^']*')*)>''', re.I,
)
ATTR = re.compile(r'''\b([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''')
REFERENCE_ID = re.compile(r'\bturn\d+(?:search|view|news|file|image)\d+\b', re.I)
CITATION_TOKEN = re.compile(r'\ue200cite[^\ue201]*\ue201')
DOCUMENT_REFERENCE = re.compile(
    r'''\bfrom document\s+(["'\x60])(?P<filename>[^\n]+?)\1[^\n]*\n'''
    r'''\s*Use ref_id\s+(["'\x60])(?P<id>turn\d+file\d+)\3\s+to reference this file''', re.I,
)


class ImportFailure(Exception):
    pass


class UnconfirmedWrite(ImportFailure):
    pass


class ArchiveEncryptionError(ImportFailure):
    pass


class WrongPassphrase(ArchiveEncryptionError):
    pass


class RequestFailure(ImportFailure):
    def __init__(self, method, route, status=None, detail=None):
        self.status = status
        self.detail = detail
        problem = f'HTTP {status}' if status is not None else 'no valid response'
        super().__init__(f'{method} {route}: {problem}')


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def chunks(handle):
    while block := handle.read(MIB):
        yield block


def hash_stream(handle, initial=b''):
    checksum = hashlib.sha256(initial)
    for block in chunks(handle):
        checksum.update(block)
    return checksum.hexdigest()


def validate_passphrase(value):
    if not value.strip() or len(value) > PASSPHRASE_LIMIT:
        raise ArchiveEncryptionError('Enter a non-empty passphrase of at most 1024 UTF-8 bytes')


def prompt_passphrase(confirm=False):
    if not sys.stdin.isatty():
        raise ArchiveEncryptionError('An interactive terminal is required for hidden passphrase input')
    first = getpass.getpass('Ключ-фраза ZIP: ').encode('utf-8')
    validate_passphrase(first)
    if confirm and getpass.getpass('Повтори ключ-фразу: ').encode('utf-8') != first:
        raise ArchiveEncryptionError('Passphrases do not match')
    return first


@contextmanager
def decrypt_zip(source, size, passphrase, limit):
    validate_passphrase(passphrase)
    if size > limit:
        raise ArchiveEncryptionError('ZIP exceeds the input size limit')
    with BytesIO() as plain:
        try:
            with pyzipper.AESZipFile(source, 'r') as archive:
                entries = archive.infolist()
                if len(entries) != 1:
                    raise ArchiveEncryptionError('Expected a ZIP encrypted by this converter')
                entry = entries[0]
                if not 0 < entry.file_size <= limit:
                    raise ArchiveEncryptionError('Invalid size of the encrypted ZIP payload')
                if entry.orig_filename == BROWSER_ENCRYPTED_PAYLOAD:
                    if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1:
                        raise ArchiveEncryptionError('Invalid encrypted browser ZIP')
                    with archive.open(entry) as content:
                        decrypt_browser_payload(content, entry.file_size, passphrase, plain)
                else:
                    if (entry.orig_filename != ENCRYPTED_PAYLOAD or entry.wz_aes_strength != 3
                            or entry.wz_aes_vendor_id != b'AE' or entry.wz_aes_version != 2
                            or entry.compress_type != zipfile.ZIP_STORED or not entry.flag_bits & 1):
                        raise ArchiveEncryptionError('Expected a ZIP encrypted by this converter')
                    with archive.open(entry, pwd=passphrase) as content:
                        for block in chunks(content):
                            if plain.tell() + len(block) > entry.file_size:
                                raise ArchiveEncryptionError('Invalid size of the encrypted ZIP payload')
                            plain.write(block)
                    if plain.tell() != entry.file_size:
                        raise ArchiveEncryptionError('The encrypted ZIP is incomplete')
        except RuntimeError:
            raise WrongPassphrase('Incorrect passphrase') from None
        except (pyzipper.BadZipFile, EOFError):
            raise ArchiveEncryptionError('The encrypted ZIP is damaged or the passphrase is incorrect') from None
        length = plain.tell()
        plain.seek(0)
        yield plain, length


def decrypt_browser_payload(content, size, passphrase, plain):
    salt_end = len(BROWSER_MAGIC) + BROWSER_SALT_LENGTH
    header_length = salt_end + BROWSER_NONCE_LENGTH
    remaining = size - header_length - BROWSER_TAG_LENGTH
    header = content.read(header_length)
    if remaining <= 0 or len(header) != header_length or not header.startswith(BROWSER_MAGIC):
        raise ArchiveEncryptionError('Invalid encrypted browser ZIP header')
    salt = header[len(BROWSER_MAGIC):salt_end]
    key = hashlib.pbkdf2_hmac('sha256', passphrase, salt, BROWSER_KDF_ROUNDS, dklen=32)
    cipher = AES.new(key, AES.MODE_GCM, nonce=header[salt_end:], mac_len=BROWSER_TAG_LENGTH)
    cipher.update(header)
    while remaining:
        block = content.read(min(MIB, remaining))
        if not block:
            raise ArchiveEncryptionError('The encrypted ZIP is incomplete')
        plain.write(cipher.decrypt(block))
        remaining -= len(block)
    tag = content.read(BROWSER_TAG_LENGTH)
    if len(tag) != BROWSER_TAG_LENGTH:
        raise ArchiveEncryptionError('The encrypted ZIP is incomplete')
    try:
        cipher.verify(tag)
    except ValueError:
        raise WrongPassphrase('Incorrect passphrase or damaged archive') from None


def path_key(name):
    return unicodedata.normalize('NFC', name).casefold()


def checked_path(name):
    parts = name.rstrip('/').split('/')
    if any(
        part in {'', '.', '..'} or part.endswith((' ', '.')) or
        re.search(r'[\\:\x00-\x1f<>"|?*]', part) or
        re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part, re.I)
        for part in parts
    ):
        raise ImportFailure('Unsafe path in export')
    return PurePosixPath(*parts)


class Export:
    def __init__(self, source, max_zip, max_expanded):
        self.source = source
        self.max_zip = max_zip
        self.max_expanded = max_expanded
        self.archive = None
        self.entries = {}

    @contextmanager
    def open(self):
        if self.source.is_dir():
            entries = ((p.relative_to(self.source).as_posix(), p, p.is_dir(), p.stat().st_size)
                       for p in self.source.rglob('*'))
            self.register(entries)
            yield self
            return
        with self.source.open('rb') as original:
            with self.open_zip(original, os.fstat(original.fileno()).st_size):
                yield self

    @contextmanager
    def open_zip(self, original, size):
        if size > self.max_zip:
            raise ImportFailure('ZIP exceeds the input size limit')
        with zipfile.ZipFile(original) as archive:
            self.archive = archive
            self.register((i.orig_filename, i, i.is_dir(), i.file_size) for i in archive.infolist())
            yield self

    def register(self, entries):
        seen = {}
        size = 0
        for name, entry, is_dir, length in entries:
            path = checked_path(name)
            key = path_key(path.as_posix())
            if key in seen or len(seen) >= 30_000:
                raise ImportFailure('Duplicate paths or too many entries in export')
            if isinstance(entry, Path) and entry.is_symlink():
                raise ImportFailure('Symbolic link in export')
            if isinstance(entry, zipfile.ZipInfo):
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in {0, stat.S_IFDIR if is_dir else stat.S_IFREG} or entry.flag_bits & 1:
                    raise ImportFailure('Encrypted entry or unsupported file type in ZIP')
            seen[key] = is_dir
            if not is_dir:
                size += length
                if size > self.max_expanded:
                    raise ImportFailure('Expanded export exceeds the size limit')
                self.entries[path] = (entry, length)
        for key in seen:
            if any(seen.get(parent.as_posix()) is False for parent in PurePosixPath(key).parents):
                raise ImportFailure('File and directory paths collide in export')

    def read(self, path):
        entry, _ = self.entries[path]
        return self.archive.open(entry) if self.archive else entry.open('rb')

    def inventory(self):
        conversations, projects, assets = {}, [], {}
        for path, (_, length) in sorted(self.entries.items()):
            name = path.name.casefold()
            directories = {part.casefold() for part in path.parts[:-1]}
            source_kind = SOURCE_JSON.fullmatch(name)
            inside_files = 'files' in directories
            structured = not inside_files and (source_kind or ('json' in directories and name.endswith('.json')))
            with self.read(path) as handle:
                if structured:
                    if length > 128 * MIB:
                        raise ImportFailure('A JSON entry exceeds 128 MiB')
                    value = json.loads(handle.read().decode('utf-8-sig'))
                    if source_kind and source_kind[1].casefold() == 'projects':
                        records = value.get('projects') if isinstance(value, dict) else value
                        if not isinstance(records, list):
                            raise ImportFailure('Invalid projects JSON structure')
                        projects.extend(row for row in records if isinstance(row, dict))
                        continue
                    if source_kind and not isinstance(value, (dict, list)):
                        raise ImportFailure('Invalid conversations JSON structure')
                    for row in value if isinstance(value, list) else [value]:
                        if not isinstance(row, dict) or not isinstance(row.get('mapping'), dict):
                            continue
                        source_id = row.get('conversation_id') or row.get('id')
                        if not isinstance(source_id, str) or not source_id.strip():
                            raise ImportFailure('Conversation has no source ID')
                        if source_id in conversations and conversations[source_id] != row:
                            raise ImportFailure('Conflicting records share a conversation ID')
                        conversations[source_id] = row
                    continue
                # Reading every entry to EOF also confirms its ZIP CRC before any API write.
                header = handle.read(24)
                checksum = hash_stream(handle, header)
                if inside_files or name.startswith(('file-', 'file_')) or (
                    not directories.intersection({'html', 'markdown', 'json'})
                    and path.suffix.casefold() not in {'.json', '.html', '.md', '.py'}
                ):
                    dimensions = None
                    if len(header) == 24 and header[:8] == b'\x89PNG\r\n\x1a\n' and header[12:16] == b'IHDR':
                        dimensions = (int.from_bytes(header[16:20], 'big'), int.from_bytes(header[20:24], 'big'))
                    assets[path] = {'sha256': checksum, 'size': length, 'dimensions': dimensions}
        return conversations, projects, assets


def pack_export(source, output, passphrase):
    validate_passphrase(passphrase)
    if output.suffix.casefold() != '.zip':
        raise ArchiveEncryptionError('The encrypted output must have a .zip extension')
    with source.open() as export:
        created = False
        try:
            handle = output.open('xb')
            created = True
            with handle:
                with pyzipper.AESZipFile(handle, 'w', compression=zipfile.ZIP_STORED) as archive:
                    archive.setpassword(passphrase)
                    archive.setencryption(pyzipper.WZ_AES, nbits=256, force_wz_aes_version=2)
                    with archive.open(ENCRYPTED_PAYLOAD, 'w', force_zip64=True) as payload:
                        if export.source.is_dir():
                            with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED) as inner:
                                for path, (entry, _) in sorted(export.entries.items()):
                                    inner.write(entry, path.as_posix())
                        else:
                            with export.source.open('rb') as original:
                                for block in chunks(original):
                                    payload.write(block)
                if handle.tell() > export.max_zip:
                    raise ArchiveEncryptionError('The encrypted ZIP exceeds the output size limit')
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise


@contextmanager
def open_import(source):
    if source.source.is_dir():
        with source.open() as export:
            yield export
        return
    passphrase = prompt_passphrase()
    with source.source.open('rb') as sealed:
        size = os.fstat(sealed.fileno()).st_size
        with decrypt_zip(sealed, size, passphrase, source.max_zip) as (plain, length):
            with source.open_zip(plain, length) as export:
                yield export


def source_name(value):
    if not isinstance(value, str) or not value:
        return ''
    return path_key(unquote(value.split('://', 1)[-1]).replace('\\', '/').rsplit('/', 1)[-1])


def asset_path_key(value):
    if not isinstance(value, str):
        return ''
    for prefix in ('attachment:', 'sandbox:/mnt/data/'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return path_key(unquote(value).replace('\\', '/').removeprefix('./'))


def match_projects(conversations, records, warnings):
    names, memberships = {}, defaultdict(set)
    for row in records:
        project = row.get('id') or row.get('project_id') or row.get('projectId')
        if not isinstance(project, str) or not project:
            warnings['project_without_id'] += 1
            continue
        title = row.get('title') or row.get('name') or 'Проект ChatGPT'
        names.setdefault(project, str(title))
        ids = row.get('conversation_ids') or row.get('conversationIds') or row.get('ids') or []
        ids = list(ids) if isinstance(ids, list) else []
        nested = row.get('conversations')
        nested = nested if isinstance(nested, list) else []
        ids.extend(item.get('conversationId') or item.get('id') for item in nested if isinstance(item, dict))
        for cid in ids:
            if not isinstance(cid, str) or not cid:
                continue
            matches = [cid] if cid in conversations else [key for key in conversations if len(cid) >= 8 and key.startswith(cid)]
            if len(matches) == 1:
                memberships[matches[0]].add(project)
            else:
                warnings['unresolved_project_membership'] += 1
    for cid, chat in conversations.items():
        project = chat.get('project_id') or chat.get('gizmo_id')
        if isinstance(project, str) and project in names:
            memberships[cid].add(project)
    selected = {}
    for cid, projects in memberships.items():
        if len(projects) == 1:
            selected[cid] = next(iter(projects))
        else:
            warnings['ambiguous_project_membership'] += 1
    return names, selected


class AssetIndex:
    def __init__(self, assets, warnings):
        self.warnings = warnings
        self.paths = {path_key(str(path)): path for path in assets}
        self.aliases = defaultdict(set)
        self.images_by_geometry = defaultdict(set)
        for path in assets:
            name = source_name(path.name)
            self.aliases[name].add(path)
            self.aliases[source_name(path.stem)].add(path)
            match = re.fullmatch(r'(file[-_][a-z0-9]+)(?:[-_](.+))?', path.stem, re.I)
            if match:
                self.aliases[path_key(match[1])].add(path)
                if match[2]:
                    self.aliases[path_key(match[2] + path.suffix)].add(path)
            info = assets[path]
            if info['dimensions']:
                self.images_by_geometry[(info['size'], *info['dimensions'])].add(path)

    @staticmethod
    def in_conversation(paths, conversation):
        if not conversation:
            return set()
        full = path_key(conversation)
        short = full[:8]
        return {path for path in paths if any(
            path_key(part) == full or (len(short) == 8 and path_key(part).endswith('_' + short))
            for part in path.parts[:-1]
        )}

    def resolve(self, ids, name=None, conversation=None, geometry=None, report=True):
        candidates = set().union(*(self.aliases[source_name(value)] for value in ids if value))
        if not candidates and name:
            exact = self.paths.get(asset_path_key(name))
            if exact is not None:
                return exact
            candidates = self.aliases[source_name(asset_path_key(name))]
        if len(candidates) > 1:
            candidates = self.in_conversation(candidates, conversation) or candidates
        if not candidates and geometry:
            candidates = self.in_conversation(self.images_by_geometry.get(geometry, ()), conversation)
        if len(candidates) == 1:
            return next(iter(candidates))
        if report:
            self.warnings['ambiguous_attachment' if candidates else 'missing_attachment'] += 1
        return None


def unix_time(value, fallback=None):
    if isinstance(value, (float, int)) and 0 < value < 10**12 and math.isfinite(value):
        return int(value)
    return fallback


def outside_code(text, transform):
    pieces, start = [], 0
    for match in MARKDOWN_CODE.finditer(text):
        pieces.extend((transform(text[start:match.start()]), match.group()))
        start = match.end()
    pieces.append(transform(text[start:]))
    return ''.join(pieces)


def reference_id(value):
    if isinstance(value, str) and REFERENCE_ID.fullmatch(value):
        return value.casefold()
    if isinstance(value, dict):
        return reference_id(f"turn{value.get('turn_index')}{value.get('ref_type')}{value.get('ref_index')}")
    return None


def web_source(record):
    url = record.get('url')
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    title = record.get('title')
    return url, title if isinstance(title, str) and title.strip() else parsed.hostname


def web_index(messages):
    candidates = defaultdict(dict)
    for message in messages:
        metadata = message.get('metadata') or {}
        groups = metadata.get('search_result_groups') or []
        references = metadata.get('content_references') or []
        entries = [entry for group in groups if isinstance(group, dict) for entry in group.get('entries') or []]
        entries.extend(item for ref in references if isinstance(ref, dict) for item in ref.get('items') or [])
        for entry in entries:
            if not isinstance(entry, dict) or not (source := web_source(entry)):
                continue
            for ref in [entry.get('ref_id'), *(entry.get('refs') or [])]:
                if ident := reference_id(ref):
                    candidates[ident][source[0]] = source
    return {ident: next(iter(values.values())) for ident, values in candidates.items() if len(values) == 1}


def document_index(messages, attached_paths):
    by_name, candidates = defaultdict(set), defaultdict(set)
    for path in attached_paths:
        by_name[source_name(path.name)].add(path)
    for message in messages:
        if (message.get('author') or {}).get('role') != 'tool':
            continue
        for part in (message.get('content') or {}).get('parts') or []:
            if not isinstance(part, str):
                continue
            for match in DOCUMENT_REFERENCE.finditer(part):
                ident, name = match['id'].casefold(), match['filename']
                candidates[ident].update(by_name[source_name(name)])
    return {ident: next(iter(paths)) for ident, paths in candidates.items() if len(paths) == 1}


def markdown_link(label, url):
    label = re.sub(r'([\\`*_\[\]<>])', r'\\\1', ' '.join(label.split()))
    target = quote(url, safe=':/?#[]@!$&\'*,;=+%~-._')
    return f'[{label}]({target})'


def source_links(sources):
    unique = {url: title for url, title in sources}
    return ', '.join(markdown_link(title, url) for url, title in unique.items())


def render_chatgpt_markdown(text, source, chat, files):
    web = chat['web_references']
    documents = chat['document_references']
    citations = {}
    for ref in source['content_references']:
        matched = ref.get('matched_text')
        if isinstance(matched, str) and CITATION_TOKEN.fullmatch(matched):
            citations[matched] = [item for row in ref.get('items') or []
                                  if isinstance(row, dict) and (item := web_source(row))]

    def lookup(text):
        return [web[ident.casefold()] for ident in REFERENCE_ID.findall(text) if ident.casefold() in web]

    def markdown(fragment):
        if not (CHATGPT_TAG.search(fragment) or CITATION_TOKEN.search(fragment)):
            return fragment

        def replace_tag(match):
            tag = match['tag'].casefold()
            attrs = {key.casefold(): double or single for key, double, single in ATTR.findall(match['attrs'])}
            if tag in {'box', 'row', 'text', 'divider'}:
                return '\n\n'
            if match['closing'] or tag in {'badge', 'favicon'}:
                return ''
            if tag == 'asyncimage':
                return attrs.get('alt', '')
            if tag == 'filecite':
                path = documents.get(attrs.get('ref_id', '').casefold())
                if path in files:
                    return markdown_link(files[path]['name'], files[path]['url'] + '?attachment=true')
                return '*(файл из экспорта)*'
            if tag == 'link':
                resolved = lookup(attrs.get('ref', ''))
                if resolved:
                    url, title = resolved[0]
                    return markdown_link(attrs.get('title') or title, url)
                return attrs.get('title', '')
            links = source_links(lookup(match['attrs']))
            return f' ({links})' if links else ''

        def replace_token(match):
            links = source_links(citations.get(match.group()) or lookup(match.group()))
            return f' ({links})' if links else ''

        fragment = CHATGPT_TAG.sub(replace_tag, fragment)
        fragment = CITATION_TOKEN.sub(replace_token, fragment)
        return re.sub(r'\n[ \t]*(?:\n[ \t]*){2,}', '\n\n', fragment)

    return outside_code(text, markdown).strip('\n') if CHATGPT_TAG.search(text) or CITATION_TOKEN.search(text) else text


def image_geometry(item):
    geometry = (item.get('size_bytes'), item.get('width'), item.get('height'))
    return geometry if all(isinstance(value, int) and value > 0 for value in geometry) else None


def visible_message(message, assets, conversation):
    role = (message.get('author') or {}).get('role')
    metadata = message.get('metadata') or {}
    content = message.get('content') or {}
    if (role not in {'user', 'assistant'} or metadata.get('is_visually_hidden_from_conversation')
            or message.get('recipient') not in {None, '', 'all'}
            or message.get('channel') == 'analysis'
            or content.get('content_type') in {'thoughts', 'reasoning_recap', 'model_editable_context'}):
        return None
    parts, attachments, links, attachment_ids = [], {}, {}, {}
    attachment_expected = False
    for item in metadata.get('attachments') or []:
        if isinstance(item, dict):
            attachment_expected = True
            ids = (item.get('id'), item.get('library_file_id'))
            path = assets.resolve(ids, item.get('name'), conversation)
            if path:
                attachments[path] = str(item.get('name') or path.name)
                for ident in ids:
                    if ident:
                        attachment_ids[source_name(ident)] = path
    for part in content.get('parts') or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            if part.get('asset_pointer'):
                attachment_expected = True
                pointer = part['asset_pointer']
                path = attachment_ids.get(source_name(pointer))
                if path is None:
                    path = assets.resolve((pointer,), conversation=conversation, geometry=image_geometry(part))
                if path:
                    attachments.setdefault(path, path.name)
            if isinstance(part.get('text'), str):
                parts.append(part['text'])
    if not parts and isinstance(content.get('text'), str):
        parts.append(content['text'])
    text = '\n'.join(parts)
    for match in LOCAL_LINK.finditer(text):
        reference = match.group(1)
        path = assets.resolve((), reference, conversation)
        if path:
            links[reference] = path
            attachments.setdefault(path, path.name)
    if not (text.strip() or attachments):
        if not attachment_expected:
            return None
        text = 'Вложение отсутствует в экспорте.'
    return {
        'role': role, 'content': text, 'timestamp': unix_time(message.get('create_time')),
        'model': metadata.get('model_slug'), 'attachments': attachments, 'links': links,
        'content_references': [ref for ref in metadata.get('content_references') or [] if isinstance(ref, dict)],
    }


def generated_image_message(message, assets, conversation):
    if (message.get('author') or {}).get('role') != 'tool':
        return None
    metadata = message.get('metadata') or {}
    if metadata.get('is_visually_hidden_from_conversation'):
        return None
    images = []
    for part in (message.get('content') or {}).get('parts') or []:
        if not isinstance(part, dict) or part.get('content_type') != 'image_asset_pointer':
            continue
        media = part.get('metadata') or {}
        if (media.get('generation') or media.get('dalle')) and not media.get('is_no_auth_placeholder'):
            images.append(part)
    if not images:
        return None
    attachments = {}
    missing = 0
    for part in images:
        media = part.get('metadata') or {}
        title = (media.get('dalle') or {}).get('serialization_title')
        ids = (part.get('asset_pointer'), media.get('asset_pointer_link'), media.get('watermarked_asset_pointer'))
        path = assets.resolve(ids, title, conversation, image_geometry(part), report=False)
        if path is None:
            missing += 1
        else:
            attachments[path] = path.name
    if missing:
        assets.warnings['missing_generated_image'] += missing
    return {
        'role': 'assistant', 'content': 'Изображение отсутствует в экспорте.' if missing else '',
        'timestamp': unix_time(message.get('create_time')), 'model': metadata.get('model_slug'),
        'attachments': attachments, 'links': {}, 'content_references': [],
    }


def convert(conversations, projects, assets, warnings):
    names, memberships = match_projects(conversations, projects, warnings)
    index = AssetIndex(assets, warnings)
    output = []
    for cid, row in conversations.items():
        mapping = row['mapping']
        if any(not isinstance(node, dict) for node in mapping.values()):
            raise ImportFailure('Invalid node in conversation mapping')
        visible = {}
        for nid, node in mapping.items():
            message = node.get('message')
            if not isinstance(message, dict):
                continue
            converted = visible_message(message, index, cid) or generated_image_message(message, index, cid)
            if converted:
                visible[nid] = converted
        if not visible:
            warnings['conversation_without_visible_messages'] += 1
            continue
        nearest = {None: None}

        def closest(node_id):
            trail, seen = [], set()
            while node_id not in nearest:
                if node_id in seen:
                    raise ImportFailure('Cycle in conversation mapping')
                seen.add(node_id)
                if node_id not in mapping:
                    warnings['missing_parent_node'] += 1
                    return None
                trail.append(node_id)
                node_id = mapping[node_id].get('parent')
            ancestor = nearest[node_id]
            for nid in reversed(trail):
                if nid in visible:
                    ancestor = nid
                nearest[nid] = ancestor
            return ancestor

        # Rank descendants by the source child order, including intervening hidden nodes.
        rank = {}
        roots = [nid for nid, node in mapping.items() if node.get('parent') not in mapping]
        stack = list(reversed(roots))
        while stack:
            nid = stack.pop()
            if nid not in mapping or nid in rank:
                continue
            rank[nid] = len(rank)
            stack.extend(reversed(mapping[nid].get('children') or []))
        for nid in mapping:
            closest(nid)
        ordered = sorted(visible, key=lambda nid: rank.get(nid, len(rank)))
        for nid in ordered:
            visible[nid]['parent'] = closest(mapping[nid].get('parent'))
        active = closest(row.get('current_node'))
        if active is None:
            active = max(ordered, key=lambda nid: visible[nid]['timestamp'] or 0)
            warnings['active_branch_inferred'] += 1
        project = memberships.get(cid)
        attached_paths = {path for message in visible.values() for path in message['attachments']}
        source_messages = [node['message'] for node in mapping.values() if isinstance(node.get('message'), dict)]
        signature = digest([row, project, {str(p): assets[p]['sha256'] for p in attached_paths}])
        output.append({
            'id': cid, 'fingerprint': signature, 'project': project, 'active': active,
            'title': str(row.get('title') or 'Чат ChatGPT'), 'messages': visible, 'order': ordered,
            'created': unix_time(row.get('create_time')), 'updated': unix_time(row.get('update_time')),
            'archived': row.get('is_archived') is True,
            'pinned': row.get('is_starred') is True or bool(row.get('pinned_time')),
            'model': row.get('default_model_slug'), 'assets': attached_paths,
            'web_references': web_index(source_messages),
            'document_references': document_index(source_messages, attached_paths),
        })
    return output, names


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebUI:
    def __init__(self, url, key, timeout):
        parsed = urlsplit(url)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.scheme not in {'https', 'http'}
                or (parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})):
            raise ImportFailure('Use HTTPS for remote Open WebUI or HTTP on loopback')
        self.url = url.rstrip('/')
        self.key = key
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @contextmanager
    def response(self, route, method='GET', body=None, headers=None):
        request = Request(self.url + route, data=body, method=method, headers={
            'Authorization': f'Bearer {self.key}', 'Accept': 'application/json', **(headers or {}),
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                yield response
        except HTTPError as error:
            detail = None
            try:
                body = json.loads(error.read(4096))
                if isinstance(body, dict):
                    detail = body.get('detail')
            except (ValueError, OSError, TransportError):
                pass
            finally:
                error.close()
            raise RequestFailure(method, route, error.code, detail) from None
        except (OSError, URLError, ValueError, TransportError):
            raise RequestFailure(method, route) from None

    def get(self, route):
        with self.response(route) as response:
            return json.load(response)

    def post(self, route, data):
        with self.response(route, 'POST', json_bytes(data), {'Content-Type': 'application/json'}) as response:
            return json.load(response)

    def chats(self):
        with self.response(API + '/chats/all') as response:
            if 'ndjson' in response.headers.get('Content-Type', ''):
                yield from (json.loads(line) for line in response if line.strip())
            else:
                records = json.load(response)
                if not isinstance(records, list):
                    raise ImportFailure('Unsupported chat listing response')
                yield from records

    def files(self):
        page = 1
        count = 0
        while True:
            result = self.get(f'{API}/files/?page={page}&content=false')
            items = result['items']
            yield from items
            count += len(items)
            if not items or count >= result['total']:
                return
            page += 1

    def file_content_available(self, ident, size):
        headers = {'Range': 'bytes=0-0'} if size else None
        try:
            with self.response(f'{API}/files/{ident}/content', headers=headers) as response:
                if response.status == 206:
                    return (response.headers.get('Content-Range') == f'bytes 0-0/{size}'
                            and bool(response.read(1)))
                length = response.headers.get('Content-Length')
                return (response.status == 200 and length == str(size)
                        and (size == 0 or bool(response.read(1))))
        except RequestFailure as error:
            if error.status == 404 and error.detail in (NOT_FOUND, 'Original file is unavailable.'):
                return False
            if error.status == 416 and error.detail is None:
                return False
            raise

    def upload(self, handle, length, name, content_type, marker):
        boundary = uuid.uuid4().hex
        filename = name.replace('"', '_').replace('\r', '_').replace('\n', '_')
        start = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
            + json_bytes({MARKER: marker}).decode('utf-8')
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            + f'Content-Type: {content_type}\r\n\r\n'
        ).encode('utf-8')
        end = f'\r\n--{boundary}--\r\n'.encode('ascii')

        def body():
            yield start
            yield from chunks(handle)
            yield end

        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(start) + length + len(end)),
        }
        with self.response(API + '/files/?process=false', 'POST', body(), headers) as response:
            return json.load(response)


@contextmanager
def journal(path):
    # Keeping this separate from SQLite transactions lets each receipt commit before the next POST.
    with path.with_suffix(path.suffix + '.lock').open('a+b') as lock_file:
        lock_file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ImportFailure('Another importer is using this journal') from None
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute('''CREATE TABLE IF NOT EXISTS receipts (
                scope TEXT, kind TEXT, source TEXT, fingerprint TEXT, remote_id TEXT,
                PRIMARY KEY (scope, kind, source)
            )''')
            yield connection
        finally:
            connection.close()


def marker_for(kind, source, fingerprint):
    return {'kind': kind, 'source': source, 'sha256': fingerprint}


def marker_key(marker):
    return marker.get('kind'), marker.get('source')


def remote_id(record, user):
    if record.get('user_id') != user:
        raise ImportFailure('Open WebUI returned an unexpected record owner')
    try:
        return str(uuid.UUID(record['id']))
    except (KeyError, ValueError, AttributeError, TypeError):
        raise ImportFailure('Open WebUI returned an invalid record ID') from None


def find_marker(record, group):
    if group == 'file':
        return (((record.get('meta') or {}).get('data') or {}).get(MARKER) or {})
    storage = 'data' if group == 'folder' else 'meta'
    return (record.get(storage) or {}).get(MARKER) or {}


class Migration:
    def __init__(self, api, user, connection, retry_unconfirmed, attachment_sizes):
        self.api = api
        self.user = user
        self.db = connection
        self.scope = f'{api.url}\n{user}'
        self.retry_unconfirmed = retry_unconfirmed
        self.attachment_sizes = attachment_sizes
        self.checked_files = {}
        self.existing = {}
        self.counts = Counter()

    def collect(self, group, rows):
        for record in rows:
            if record.get('user_id') != self.user:
                continue
            marker = find_marker(record, group)
            key = marker_key(marker)
            if key[0] not in {'conversation', 'attachment', 'project'} or not key[1]:
                continue
            ident = remote_id(record, self.user)
            if group == 'file':
                stored_hash = (record.get('meta') or {}).get('file_hash')
                if stored_hash != key[1] or stored_hash != marker.get('sha256'):
                    continue
                if stored_hash not in self.attachment_sizes or key in self.existing:
                    continue
                if not self.file_available(ident, stored_hash):
                    continue
            if key in self.existing and remote_id(self.existing[key], self.user) != ident:
                raise ImportFailure('Multiple server records share an importer marker')
            self.existing[key] = record

    def file_available(self, ident, checksum):
        if ident not in self.checked_files:
            self.checked_files[ident] = self.api.file_content_available(ident, self.attachment_sizes[checksum])
        return self.checked_files[ident]

    def folders(self):
        rows = self.api.get(API + '/folders/')
        return [self.api.get(API + '/folders/' + remote_id(row, self.user)) for row in rows
                if row.get('user_id') == self.user]

    def refresh(self, group):
        rows = {'chat': self.api.chats, 'file': self.api.files, 'folder': self.folders}[group]()
        self.collect(group, rows)
        return rows

    def receipt(self, marker, ident):
        self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?, ?, ?, ?, ?)', (
            self.scope, *marker_key(marker), marker['sha256'], ident,
        ))

    def get_confirmed(self, group, ident):
        resource = {'chat': 'chats', 'file': 'files', 'folder': 'folders'}[group]
        try:
            record = self.api.get(f'{API}/{resource}/{uuid.UUID(ident)}')
        except RequestFailure as error:
            # Open WebUI 0.13.4 returns 401 for a missing chat and 404 for other missing resources.
            missing_status = error.status == 404 or (group == 'chat' and error.status == 401)
            if missing_status and error.detail == NOT_FOUND:
                return None
            raise
        if remote_id(record, self.user) != ident:
            raise ImportFailure('Open WebUI returned a different record for a confirmed ID')
        return record

    def ensure(self, group, marker, create, prepare=None):
        key = marker_key(marker)
        record = self.existing.get(key)
        if record is not None:
            ident = remote_id(record, self.user)
            previous = find_marker(record, group)
            self.receipt(previous, ident)
            self.counts[f'{key[0]}_unchanged' if previous.get('sha256') == marker['sha256']
                        else f'{key[0]}_changed'] += 1
            return ident
        prior = self.db.execute('SELECT fingerprint, remote_id FROM receipts WHERE scope=? AND kind=? AND source=?',
                                (self.scope, *key)).fetchone()
        if prior and prior[1]:
            previous_hash, ident = prior
            record = self.get_confirmed(group, ident)
            if record is not None and group == 'file' and not self.file_available(ident, marker['sha256']):
                record = None
            if record is not None:
                previous = find_marker(record, group)
                if previous and marker_key(previous) != key:
                    raise ImportFailure('A confirmed ID belongs to a different import record')
                if group == 'file' and (record.get('meta') or {}).get('file_hash') != marker['sha256']:
                    raise ImportFailure('A confirmed attachment has a different SHA-256')
                if previous:
                    previous_hash = previous.get('sha256') or previous_hash
                    self.existing[key] = record
                self.counts[f'{key[0]}_unchanged' if previous_hash == marker['sha256'] else f'{key[0]}_changed'] += 1
                return ident
        elif prior and not self.retry_unconfirmed:
            raise UnconfirmedWrite(f'Previous {key[0]} request is unresolved. See --retry-unconfirmed in --help')
        if prepare:
            prepare()
        self.receipt(marker, None)
        try:
            record = create()
            ident = remote_id(record, self.user)
            if find_marker(record, group) != marker:
                raise ImportFailure('Open WebUI did not preserve the importer marker')
            if group == 'file' and (record.get('meta') or {}).get('file_hash') != marker['sha256']:
                raise ImportFailure('Open WebUI received a file with a different SHA-256')
        except ImportFailure as error:
            self.refresh(group)
            record = self.existing.get(key)
            if record is None and isinstance(error, RequestFailure) and error.status in {400, 401, 403, 404, 405, 413, 415, 422}:
                self.db.execute('DELETE FROM receipts WHERE scope=? AND kind=? AND source=?', (self.scope, *key))
                raise
            if record is None or find_marker(record, group) != marker:
                raise UnconfirmedWrite(f'{key[0]} write outcome is unresolved. Rerun to reconcile with Open WebUI') from None
            ident = remote_id(record, self.user)
        self.receipt(marker, ident)
        if group == 'file' and not self.file_available(ident, marker['sha256']):
            raise ImportFailure('Open WebUI saved the attachment record but its file is unavailable')
        self.existing[key] = record
        self.counts[f'{key[0]}_written'] += 1
        return ident

    def import_chat(self, payload):
        result = self.api.post(API + '/chats/import', {'chats': [payload]})
        if not isinstance(result, list) or len(result) != 1:
            raise ImportFailure('Unexpected response from chat import')
        return result[0]

    def file(self, handle, name, content_type, size, checksum):
        marker = marker_for('attachment', checksum, checksum)
        return self.ensure('file', marker, lambda: self.api.upload(handle, size, name, content_type, marker))


def file_reference(ident, name, mime, length):
    return {
        'id': ident, 'name': name, 'type': 'image' if mime.startswith('image/') else 'file',
        'content_type': mime, 'size': length, 'status': 'uploaded',
        'url': f'{API}/files/{ident}/content',
    }


def message_id(user, source, node):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json_bytes(['chatgpt-import', user, source, node]).decode('utf-8')))


def chat_payload(chat, user, refs, marker, folder_id=None):
    ids = {node: message_id(user, chat['id'], node) for node in chat['order']}
    history = {}
    for node in chat['order']:
        source = chat['messages'][node]
        contents = source['content']
        if source['role'] == 'assistant':
            contents = render_chatgpt_markdown(contents, source, chat, refs)

        def local_link(match):
            path = source['links'].get(match.group(1))
            return refs[path]['url'] + '?attachment=true' if path in refs else match.group(0)

        contents = outside_code(contents, lambda text: LOCAL_LINK.sub(local_link, text))
        message = {
            'id': ids[node], 'parentId': ids.get(source['parent']), 'childrenIds': [],
            'role': source['role'], 'content': contents,
            'timestamp': source['timestamp'] or chat['created'] or 1,
        }
        if source['attachments']:
            message['files'] = []
            for path, name in source['attachments'].items():
                ref = {**refs[path], 'name': name}
                if source['role'] == 'user' and ref['type'] == 'image':
                    ref['url'] = ref['id']
                message['files'].append(ref)
        if source['role'] == 'assistant':
            message['done'] = True
            if model := source['model'] or chat['model']:
                message.update(model=model, modelName=model)
        history[message['id']] = message
    for message in history.values():
        if message['parentId'] is not None:
            history[message['parentId']]['childrenIds'].append(message['id'])
    current = ids[chat['active']]
    branch, cursor = [], current
    while cursor is not None:
        branch.append(history[cursor])
        cursor = history[cursor]['parentId']
    return {
        'chat': {'title': chat['title'], 'models': [], 'history': {'currentId': current, 'messages': history},
                 'messages': list(reversed(branch))},
        'current_message_id': current, 'folder_id': folder_id,
        'created_at': chat['created'], 'updated_at': chat['updated'] or chat['created'],
        'archived': chat['archived'], 'pinned': chat['pinned'],
        'meta': {MARKER: marker, 'chatgpt_model': chat['model']},
    }


def server_preflight(api, args, upload_sizes, need_folders, user=None):
    from_session = user is not None
    if user is None:
        user = api.get(API + '/users/user/status')
    if user.get('id') != args.user_id:
        raise ImportFailure('The API key belongs to a different Open WebUI user')
    permissions = api.get(API + '/users/permissions')
    if user.get('role') != 'admin':
        chat = permissions.get('chat') or {}
        if not chat.get('import') or (chat.get('temporary') and chat.get('temporary_enforced')):
            raise ImportFailure('Chat import is not permitted for this user')
        if need_folders and not (permissions.get('features') or {}).get('folders'):
            raise ImportFailure('Folders are not permitted for this user')
    limit = args.upload_limit_mib * MIB
    if from_session:
        configured = (api.get('/api/config').get('file') or {}).get('max_size')
        if configured:
            limit = min(limit, int(configured) * MIB)
    elif user.get('role') == 'admin':
        try:
            configured = api.get(API + '/retrieval/config').get('FILE_MAX_SIZE')
        except RequestFailure as error:
            if error.status != 403:
                raise
            configured = None
        if configured:
            limit = min(limit, int(configured) * MIB)
    if any(size > limit for size in upload_sizes):
        raise ImportFailure(f'An upload exceeds the effective {limit // MIB} MiB limit')


def apply_import(args, export, chats, projects, assets, api=None, user=None):
    if api is None:
        key = os.environ.get('OPEN_WEBUI_API_KEY')
        if not key and sys.stdin.isatty():
            key = getpass.getpass('Open WebUI API key: ')
        if not key or not key.strip():
            raise ImportFailure('An Open WebUI API key is required')
        api = WebUI(args.url, key.strip(), args.timeout)
    associated = {path for chat in chats for path in chat['assets']}
    server_preflight(api, args, [assets[p]['size'] for p in associated], bool(projects), user)
    attachment_sizes = {assets[path]['sha256']: assets[path]['size'] for path in associated}

    with journal(args.journal) as database:
        migration = Migration(api, args.user_id, database, args.retry_unconfirmed, attachment_sizes)
        migration.refresh('chat')
        migration.refresh('file')
        folders = migration.refresh('folder') if projects else []
        occupied_names = {path_key(folder['name']) for folder in folders if folder.get('parent_id') is None}

        folder_ids = {}
        for project, title in sorted(projects.items()):
            marker = marker_for('project', project, digest(title))
            clean_name = title.strip() or 'Проект ChatGPT'
            name, counter = clean_name, 0
            while path_key(name) in occupied_names:
                counter += 1
                suffix = digest(project)[:8] if counter == 1 else f'{digest(project)[:8]}-{counter}'
                name = f'{clean_name} (ChatGPT {suffix})'
            folder_ids[project] = migration.ensure('folder', marker, lambda: api.post(API + '/folders/', {
                'name': name, 'parent_id': None, 'data': {MARKER: marker},
            }))
            occupied_names.add(path_key(name))

        references = {}
        for chat in chats:
            marker = marker_for('conversation', chat['id'], chat['fingerprint'])

            def prepare_files():
                for path in sorted(chat['assets']):
                    if path not in references:
                        info = assets[path]
                        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                        with export.read(path) as handle:
                            ident = migration.file(handle, path.name, mime, info['size'], info['sha256'])
                        references[path] = file_reference(ident, path.name, mime, info['size'])

            def write_chat():
                payload = chat_payload(chat, args.user_id, references, marker, folder_ids.get(chat['project']))
                return migration.import_chat(payload)

            migration.ensure('chat', marker, write_chat, prepare_files)
        return {'import': dict(migration.counts)}


def summarize(conversations, chats, projects, assets, warnings):
    matched = {path for chat in chats for path in chat['assets']}
    return {
        'conversations_found': len(conversations), 'conversations_ready': len(chats),
        'messages_ready': sum(len(chat['messages']) for chat in chats), 'projects': len(projects),
        'files_found': len(assets), 'files_linked': len(matched),
        'files_unlinked': len(assets) - len(matched), 'warnings': dict(warnings),
    }


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Export directory or ZIP archive')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Inspect the export locally without calling Open WebUI')
    mode.add_argument('--apply', action='store_true', help='Import an encrypted ZIP into an Open WebUI account')
    mode.add_argument('--pack', action='store_true', help='Create an AES-256 ZIP from an export directory or plain ZIP')
    parser.add_argument('--output', type=Path, help='New encrypted ZIP path, required for --pack')
    parser.add_argument('--url', default='http://127.0.0.1:3000', help='Open WebUI base URL')
    parser.add_argument('--user-id', help='Expected Open WebUI account UUID, required for --apply')
    parser.add_argument('--upload-limit-mib', type=positive_int,
                        help='Approved per-file limit in MiB, accounting for Open WebUI and reverse proxy limits. Required for --apply')
    parser.add_argument('--max-zip-mib', type=positive_int, default=256, help='Maximum ZIP size in MiB (default: 256)')
    parser.add_argument('--max-expanded-mib', type=positive_int, default=2048,
                        help='Maximum expanded size in MiB (default: 2048)')
    parser.add_argument('--timeout', type=positive_int, default=120, help='HTTP timeout in seconds (default: 120)')
    parser.add_argument('--journal', type=Path, default=Path('.chatgpt-import.sqlite3'),
                        help='Persistent server-side receipt database. Reuse this path across runs')
    parser.add_argument('--retry-unconfirmed', action='store_true',
                        help='Retry a missing record only after an administrator confirms the original POST has finished and did not commit')
    args = parser.parse_args()
    if args.pack != (args.output is not None):
        parser.error('--pack and --output must be used together')
    if args.apply and (args.source.is_dir() or not args.user_id or not args.upload_limit_mib):
        parser.error('--apply requires a ZIP, --user-id and --upload-limit-mib')
    if args.apply:
        try:
            args.user_id = str(uuid.UUID(args.user_id))
        except ValueError:
            parser.error('--user-id must be a UUID')

    warnings = Counter()
    try:
        source = Export(args.source, args.max_zip_mib * MIB, args.max_expanded_mib * MIB)
        if args.pack:
            pack_export(source, args.output, prompt_passphrase(confirm=True))
            print(f'Создан зашифрованный ZIP: {args.output}')
            return 0
        with open_import(source) as export:
            conversations, project_rows, assets = export.inventory()
            chats, projects = convert(conversations, project_rows, assets, warnings)
            print(json.dumps(summarize(conversations, chats, projects, assets, warnings),
                             ensure_ascii=False, sort_keys=True), flush=True)
            if args.apply:
                result = apply_import(args, export, chats, projects, assets)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except KeyboardInterrupt:
        return 130
    except ImportFailure as error:
        print(f'Import stopped: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, EOFError,
            NotImplementedError, zipfile.BadZipFile, sqlite3.Error) as error:
        print(f'Import stopped: invalid export, server response, or local I/O ({type(error).__name__})', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
