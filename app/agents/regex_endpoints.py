"""Optional exact-ripgrep replay; original pages survive every supplement failure."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_FILES = 64
MAX_ENTRIES = 512
MAX_WIRE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 8_000
SCAN_SECONDS = 5.0


class RegexEndpointBatch:
    def __init__(self, root: Path):
        self.root = root
        self.lock = asyncio.Lock()
        self.deadline = None
        self.sources = {}
        self.source_bytes = 0
        self.output_bytes = 0

    def check_time(self):
        if time.monotonic() > self.deadline:
            raise ValueError('endpoint scan deadline')

    def snapshot(self, target):
        self.check_time()
        target.relative_to((self.root / 'logs').resolve())
        files, entries = [], 0
        pending = [target]
        while pending:
            self.check_time()
            path = pending.pop()
            entries += 1
            if entries > MAX_ENTRIES or path.is_symlink():
                raise ValueError('scope entries/symlink limit')
            if path.is_dir():
                with os.scandir(path) as children:
                    for child in children:
                        if child.name == '.git':
                            continue
                        if entries + len(pending) >= MAX_ENTRIES:
                            raise ValueError('scope entries limit')
                        pending.append(Path(child.path))
            elif path.is_file():
                files.append(path)
            else:
                raise ValueError('nonregular scope')
        out = {}
        for path in sorted(files):
            self.check_time()
            stat = path.stat()
            if path not in self.sources:
                if len(self.sources) >= MAX_FILES or self.source_bytes + stat.st_size > MAX_SOURCE_BYTES:
                    raise ValueError('source budget')
                self.sources[path] = stat.st_size
                self.source_bytes += stat.st_size
            if stat.st_size != self.sources[path]:
                raise ValueError('source size changed')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(64 * 1024):
                    self.check_time()
                    digest.update(chunk)
            if path.stat() != stat:
                raise ValueError('source changed')
            out[path.relative_to(self.root).as_posix()] = (
                stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, digest.hexdigest())
        return out

    async def search(self, request):
        from app.agents.evidence_tools import _path, search_page
        async with self.lock:
            if self.deadline is None:
                self.deadline = time.monotonic() + SCAN_SECONDS
            before = None
            try:
                target = _path(self.root, request.get('path', '.'))
                before = await asyncio.to_thread(self.snapshot, target)
            except (OSError, ValueError, TypeError):
                pass
            original = await search_page(self.root, request)
            if before is not None and not original.get('warning'):
                try:
                    extra = await self.replay(request, original, before)
                    if before != await asyncio.to_thread(self.snapshot, target):
                        raise ValueError('source changed')
                    if extra['rows']:
                        original['regex_endpoints'] = extra
                except (OSError, ValueError, KeyError, IndexError, asyncio.TimeoutError):
                    pass
            return original

    async def replay(self, request, original, before):
        from app.agents.evidence_tools import _path, _strip_line_ending, search_arguments
        self.check_time()
        process = await asyncio.create_subprocess_exec(
            *search_arguments(self.root, request), cwd=self.root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=2 * 1024 * 1024)
        async def drain():
            error = False
            while chunk := await process.stderr.read(4096):
                error = True
            return error
        errors = asyncio.create_task(drain())
        rows, ends = [], {}
        offset, limit = request.get('offset', 0), request.get('limit', 20)
        count, wire = 0, 0
        async def consume():
            nonlocal count, wire
            async for raw in process.stdout:
                wire += len(raw)
                if wire > MAX_WIRE_BYTES:
                    raise ValueError('match output budget')
                event = json.loads(raw)
                if event.get('type') != 'match':
                    continue
                data = event['data']
                name, text = data['path'].get('text'), data['lines'].get('text')
                if name is None or text is None:
                    raise ValueError('non-UTF8 match')
                safe = _path(self.root, name).relative_to(self.root).as_posix()
                if safe not in before:
                    raise ValueError('unexpected source')
                text = _strip_line_ending(text)
                number = data['line_number']
                first = len(text.encode()[:data['submatches'][0]['start']].decode(errors='replace'))
                begin = max(0, first - 100) if len(text) > 600 else 0
                excerpt = text[begin:begin + 600]
                count += 1
                if offset < count <= offset + limit:
                    rows.append(dict(path=safe, line=number, column=begin, text=excerpt,
                                     line_excerpted=begin > 0 or len(excerpt) < len(text),
                                     read_request=dict(path=safe, start_line=number, line_count=20)))
                if safe not in ends:
                    ends[safe] = dict(count=0, first=(number, text))
                ends[safe].update(count=ends[safe]['count'] + 1, last=(number, text))
            await process.wait()
        try:
            await asyncio.wait_for(consume(), max(0.001, self.deadline - time.monotonic()))
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            error = await errors
        if process.returncode not in (0, 1) or error or rows != original['matches']:
            raise ValueError('original page mismatch or scan error')
        if original['complete'] != (count <= offset + limit):
            raise ValueError('continuation mismatch')
        result = dict(rows=[], omitted_endpoints=0, matching_lines_total=count, scan_complete=True,
                      event_history_complete=False,
                      order='native file/LF line order; not chronological extrema or causal proof')
        visible = {(r['path'], r['line']) for r in rows if not r['line_excerpted']}
        for window in original.get('context_windows', []):
            visible.update((window['path'], r['line']) for r in window['lines'])
        for path, value in ends.items():
            for number, text in dict([value['first'], value['last']]).items():
                if (path, number) in visible:
                    continue
                item = dict(path=path, line=number, text=text, matching_lines_in_file=value['count'],
                            source_sha256=before[path][-1])
                result['rows'].append(item)
                if len(json.dumps(result, ensure_ascii=False).encode()) + self.output_bytes > MAX_OUTPUT_BYTES - 200:
                    result['rows'].pop()
                    result['omitted_endpoints'] += 1
        self.output_bytes += len(json.dumps(result, ensure_ascii=False).encode())
        return result
