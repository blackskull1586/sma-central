"""Owner-only archive browser and confirmed deletion of finished recordings."""
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, QueryDict, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_http_methods, require_safe
from .access import access
from .models import AuditEvent, Channel

TZ = dt.timezone(dt.timedelta(hours=-4))
STATES = {'recording': 'Gravando', 'pending_export': 'Preparando vídeo', 'complete': 'Disponível', 'deleting': 'Exclusão pendente'}
LOG = logging.getLogger(__name__)

def root():
    return Path(getattr(settings, 'SMA_RECORDINGS_DIR', '/var/lib/sma-recordings'))

def safe_open(relative):
    """Reject traversal and symlinks at every path component, including races."""
    parts = Path(relative).parts
    if not parts or any(x in ('', '.', '..', '/') for x in parts) or Path(relative).is_absolute():
        raise OSError('invalid archive path')
    fd = os.open(root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise OSError('not a regular file')
        return os.fdopen(result, 'rb')
    finally:
        os.close(fd)

def entries():
    rows = []
    # An archive keeps its original scope. Deleted or reassigned channels cannot
    # silently make files available under a different environment or unit.
    scopes = {
        str(channel): (str(environment), str(unit) if unit else None)
        for channel, environment, unit in
        Channel.objects.values_list('id', 'environment_id', 'unit_id')
    }
    for path in root().glob('*/*/*/manifest.json'):
        try:
            relative = path.relative_to(root())
            with safe_open(relative) as f:
                if os.fstat(f.fileno()).st_size > 4 * 1024 * 1024:
                    continue
                m = json.load(f)
            if not isinstance(m, dict):
                continue
            rid = m['recording_id']
            if not re.fullmatch('[0-9a-f]{32}', rid) or path.parent.name != rid:
                continue
            # Unknown source metadata cannot publish an archive item.
            channel_id = str(uuid.UUID(m['channel_id']))
            environment_id = str(uuid.UUID(m['environment_id']))
            unit_id = str(uuid.UUID(m['unit_id'])) if m.get('unit_id') else None
            if scopes.get(channel_id) != (environment_id, unit_id):
                continue
            started = dt.datetime.fromisoformat(m['started_at'])
            if started.tzinfo is None or m['status'] not in STATES:
                continue
            started = started.astimezone(TZ)
            seconds = max(0, int(float(m.get('duration_seconds', 0))))
            m.update(url_id=str(uuid.UUID(rid)), folder=relative.parent, started=started, day=started.date().isoformat(),
                     gdh=started.strftime('%d/%m/%Y %H:%M:%S'),
                     duration=f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}',
                     state_label=STATES[m['status']], playable=False,
                     deletable=m['status'] in ('complete', 'deleting'))
            filename = m.get('video_file', '')
            if m['status'] == 'complete' and filename and Path(filename).name == filename and filename.endswith('.mp4'):
                with safe_open(m['folder'] / filename) as f:
                    size = os.fstat(f.fileno()).st_size
                if size > 0:
                    m.update(playable=True, size_label=f'{size / (1024 * 1024):.1f} MB')
            rows.append(m)
        except (OSError, ValueError, KeyError, TypeError, OverflowError, AttributeError):
            continue
    return sorted(rows, key=lambda x: (x['started'], x['recording_id']), reverse=True)

def owner_portal(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.is_owner_portal:
            raise Http404
        return view(request, *args, **kwargs)
    return access(owner=True, scope=False)(wrapped)

def central(view):
    return require_safe(owner_portal(view))

def filter_query(raw):
    """Retain only list filters; return targets are always local to the archive."""
    source = QueryDict(raw[:2048])
    clean = QueryDict('', mutable=True)
    for key in ('servidor', 'busca', 'inicio', 'fim', 'pagina'):
        value = source.get(key, '').strip()[:100]
        if value:
            clean[key] = value
    return clean.urlencode()

def lookup(recording_id):
    for row in entries():
        if row['recording_id'] == recording_id.hex:
            return row
    raise Http404

@central
def recordings(request):
    rows = entries()
    servers = sorted({(x['environment_id'], x.get('environment_name') or x.get('environment_slug', 'SMA')) for x in rows}, key=lambda x: x[1])
    server = request.GET.get('servidor', '')
    query = request.GET.get('busca', '').strip()[:100]
    start = request.GET.get('inicio', '')
    end = request.GET.get('fim', '')
    for key, value in [('inicio', start), ('fim', end)]:
        try:
            if value: dt.date.fromisoformat(value)
        except ValueError:
            if key == 'inicio': start = ''
            else: end = ''
    if server: rows = [x for x in rows if x['environment_id'] == server]
    if query: rows = [x for x in rows if query.casefold() in ' '.join(str(x.get(k) or '') for k in ('channel_name', 'unit_name', 'operator_name')).casefold()]
    if start: rows = [x for x in rows if x['day'] >= start]
    if end: rows = [x for x in rows if x['day'] <= end]
    page = Paginator(rows, 20).get_page(request.GET.get('pagina'))
    params = request.GET.copy(); params.pop('pagina', None)
    return render(request, 'recordings.html', dict(recordings=page, servers=servers, server=server, query=query, start=start, end=end, filters=params.urlencode(), total=len(rows), return_query=filter_query(request.GET.urlencode())))

@central
def recording_detail(request, recording_id):
    row = lookup(recording_id)
    return render(request, 'recording-detail.html', {'recording': row})

@contextmanager
def archive_directory(relative):
    parts = Path(relative).parts
    if len(parts) != 3 or Path(relative).is_absolute() or any(p in ('', '.', '..') for p in parts) or not re.fullmatch('[0-9a-f]{32}', parts[-1]):
        raise OSError('invalid archive directory')
    fd = os.open(root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)

def write_manifest(fd, data):
    name = '.delete-' + uuid.uuid4().hex + '.tmp'
    file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(file_fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, 'manifest.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try: os.unlink(name, dir_fd=fd)
        except FileNotFoundError: pass

def delete_finished_recording(row, actor):
    """Keep a tombstone so concurrent recorder scans never lose their manifest."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError('safe directory deletion unavailable')
    with archive_directory(row['folder']) as fd:
        lock = os.open('.delete.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise OSError('invalid lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            mfd = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(mfd, 'r') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or os.fstat(f.fileno()).st_size > 4 * 1024 * 1024:
                    raise OSError('invalid manifest')
                data = json.load(f)
            if data.get('recording_id') != row['recording_id'] or data.get('status') not in ('complete', 'deleting'):
                raise OSError('recording is not finished')
            if not Channel.objects.filter(pk=data['channel_id'], environment_id=data['environment_id'], unit_id=data.get('unit_id')).exists():
                raise OSError('recording scope changed')
            scope = dict(actor=actor, environment_id=data['environment_id'], unit_id=data.get('unit_id'), owner_only=True, target='Gravação ' + data['recording_id'])
            AuditEvent.objects.create(action='Exclusão de gravação solicitada', **scope)
            data.update(status='deleting', deletion_requested_at=dt.datetime.now(TZ).isoformat(), deletion_requested_by=actor.pk)
            write_manifest(fd, data)
            # The archive namespace belongs to this recording. rmtree uses
            # descriptor-based traversal; a symlink is unlinked, never followed.
            for name in os.listdir(fd):
                if name in ('manifest.json', '.delete.lock'):
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        shutil.rmtree(name, dir_fd=fd)
                    else:
                        os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    # The recorder may just have removed redundant segments.
                    continue
            AuditEvent.objects.create(action='Gravação excluída', **scope)
            data.update(status='deleted', deleted_at=dt.datetime.now(TZ).isoformat(), deleted_by=actor.pk)
            write_manifest(fd, data)
        finally:
            os.close(lock)

@require_http_methods(['GET', 'HEAD', 'POST'])
@owner_portal
def recording_delete(request, recording_id):
    row = lookup(recording_id)
    return_query = filter_query((request.POST if request.method == 'POST' else request.GET).get('retorno', ''))
    back = reverse('recordings') + ('?' + return_query if return_query else '')
    context = {'recording': row, 'return_query': return_query, 'back_url': back}
    if not row['deletable']:
        return render(request, 'recording-delete.html', {**context, 'error': 'Aguarde a gravação terminar e o vídeo ficar disponível para excluir.'}, status=409)
    if request.method == 'POST':
        if request.POST.get('confirmation') != str(recording_id):
            return render(request, 'recording-delete.html', {**context, 'error': 'Confirme a exclusão pelo botão abaixo.'}, status=400)
        try:
            delete_finished_recording(row, request.user)
        except (OSError, ValueError, KeyError, TypeError):
            LOG.exception('Recording deletion incomplete: %s', recording_id)
            return render(request, 'recording-delete.html', {**context, 'error': 'Não foi possível concluir a exclusão. Atualize a lista e tente novamente.'}, status=409)
        messages.success(request, 'Vídeo excluído. Os filtros foram mantidos.')
        return redirect(back)
    return render(request, 'recording-delete.html', context)

class Slice:
    def __init__(self, f, start, count):
        self.f, self.count = f, count
        f.seek(start)
    def __iter__(self):
        try:
            while self.count > 0:
                data = self.f.read(min(self.count, 256 * 1024))
                if not data: break
                self.count -= len(data)
                yield data
        finally:
            self.close()
    def close(self):
        self.f.close()

@central
def recording_media(request, recording_id, download=False):
    row = lookup(recording_id)
    if not row['playable']: raise Http404
    try:
        f = safe_open(row['folder'] / row['video_file'])
    except OSError:
        raise Http404
    size = os.fstat(f.fileno()).st_size
    if size <= 0:
        f.close()
        raise Http404
    start, end, status = 0, size - 1, 200
    # Range applies to GET only; unsupported multi-ranges fail closed.
    header = request.headers.get('Range') if request.method == 'GET' else None
    if header:
        try:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
            if not match or not any(match.groups()): raise ValueError
            a, b = match.groups()
            if a:
                start = int(a); end = min(int(b), size - 1) if b else size - 1
            else:
                count = int(b)
                if count <= 0: raise ValueError
                start = max(0, size - count)
            if start >= size or end < start: raise ValueError
            status = 206
        except ValueError:
            f.close()
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
    length = end - start + 1
    if request.method == 'HEAD':
        f.close(); response = HttpResponse(content_type='video/mp4')
    else:
        response = StreamingHttpResponse(Slice(f, start, length), status=status, content_type='video/mp4')
    response['Content-Length'] = str(length)
    response['Accept-Ranges'] = 'bytes'
    response['Content-Disposition'] = content_disposition_header(download, row['video_file'])
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Accel-Buffering'] = 'no'
    if status == 206: response['Content-Range'] = f'bytes {start}-{end}/{size}'
    return response

@central
def recordings_css(request):
    return HttpResponse('''.recording-filters{display:flex;flex-wrap:wrap;gap:16px;align-items:end;margin-bottom:24px}.recording-filters label{flex:1;min-width:160px}.recording-filters input,.recording-filters select{width:100%}.recording-player{display:block;width:100%;max-height:70vh;background:#030507;aspect-ratio:16/9}.recording-meta{display:flex;flex-wrap:wrap;gap:16px 32px}.recording-meta dt{color:var(--muted);font-size:12px}.recording-meta dd{margin:6px 0 0}.recording-note{margin:16px 0}.recording-pagination{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-top:24px}.recording-actions{display:flex;flex-wrap:wrap;gap:12px}.recording-empty{padding:32px 0}.recording-sub{display:block;font-size:12px;color:var(--muted);margin-top:6px}''', content_type='text/css')
