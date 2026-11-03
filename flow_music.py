"""Local Flow Music controller. Run: python flow_music.py --open"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Literal
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from flow_music_browser import HOME, ROOT, SITE, DEFAULT_ACCOUNT, open_chrome, profile_directory

TERMINAL = {'done', 'failed', 'cancelled', 'interrupted'}
PHASES = {'opening', 'submitting', 'submitted', 'downloading'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, obj):
    partial = path.with_suffix('.json.new')
    partial.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(partial, path)


class NewJob(BaseModel):
    prompt: str = Field(min_length=5, max_length=6000)
    title: str = Field(default='Quiet afternoon', max_length=120)
    output_dir: str = Field(default=str(ROOT / 'data/musics/flowmusic'), max_length=1000)
    format: Literal['mp3', 'wav'] = 'mp3'
    instrumental: bool = True
    timeout: int = Field(default=600, ge=30, le=1800)


class Event(BaseModel):
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    phase: str = ''
    message: str = Field(default='', max_length=2000)
    session_url: str = Field(default='', max_length=1000)
    source: str = Field(default='', max_length=2000)
    text: str = Field(default='', max_length=16000)
    submitted: bool = False


class MusicStore:
    def __init__(self, home: Path, *, port=7861, account=DEFAULT_ACCOUNT):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.jobs = self.home / 'jobs'
        self.jobs.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.connected_at = 0
        self.port, self.account = port, account
        token_path = self.home / 'bridge-token'
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32), encoding='ascii')
        self.token = token_path.read_text(encoding='ascii').strip()
        self.extension = self.home / 'extension'
        self.extension.mkdir(exist_ok=True)
        for source in (ROOT / 'browser_extensions/flow_music').iterdir():
            if source.is_file():
                shutil.copyfile(source, self.extension / source.name)
        (self.extension / 'config.js').write_text(
            'const LOFI_BRIDGE = ' + json.dumps({'url': f'http://127.0.0.1:{port}', 'token': self.token}) + ';\n', encoding='utf-8')
        # A previous process may have stopped after sending a prompt. Never resubmit it.
        for path in self.jobs.glob('*/job.json'):
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['status'] not in TERMINAL and job['status'] != 'queued':
                job.update(status='interrupted', message='Local tool restarted. Check the saved Flow Music session before generating again.', updated_at=stamp())
                write_json(path, job)

    def path(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise HTTPException(400, 'Invalid job ID')
        return self.jobs / job_id / 'job.json'

    def get(self, job_id):
        with self.lock:
            path = self.path(job_id)
            if not path.is_file():
                raise HTTPException(404, 'Job not found')
            return json.loads(path.read_text(encoding='utf-8'))

    def save(self, job):
        with self.lock:
            job['updated_at'] = stamp()
            path = self.path(job['id'])
            path.parent.mkdir(exist_ok=True)
            write_json(path, job)
        return job

    def all(self):
        with self.lock:
            return sorted([json.loads(p.read_text(encoding='utf-8')) for p in self.jobs.glob('*/job.json')],
                          key=lambda job: job['created_at'], reverse=True)

    def create(self, req: NewJob):
        if not req.prompt.strip():
            raise HTTPException(400, 'Prompt cannot be blank')
        folder = Path(req.output_dir).expanduser().resolve()
        if folder.exists() and not folder.is_dir():
            raise HTTPException(400, 'Output must be a folder')
        with self.lock:
            if any(job['status'] not in TERMINAL for job in self.all()):
                raise HTTPException(409, 'Finish or cancel the current job first')
            folder.mkdir(parents=True, exist_ok=True)
            job = {**req.model_dump(), 'prompt': req.prompt.strip(), 'output_dir': str(folder),
                   'id': uuid.uuid4().hex, 'status': 'queued', 'phase': 'queued',
                   'created_at': stamp(), 'session_url': '', 'cancel_requested': False,
                   'submitted': False, 'output': '', 'message': 'Đang đợi extension. Giữ tab Flow Music mở.'}
            return self.save(job)

    def download(self, event: Event):
        with self.lock:
            job = self.get(event.id)
            if job['status'] != 'running' or job['phase'] != 'downloading' or job['cancel_requested']:
                raise HTTPException(409, 'This job is not accepting a download')
            source = Path(event.source).resolve()
            # Only accept the new file which the extension named for this job.
            if source.parent.name != job['id'] or source.parent.parent.name != 'LoFiFlowMusic':
                raise HTTPException(400, 'Not this job\'s Chrome download')
            if source.suffix.lower() != '.' + job['format'] or not source.is_file():
                raise HTTPException(400, 'Wrong format or missing downloaded file')
            info = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                'stream=codec_type:format=duration', '-of', 'json', str(source)],
                capture_output=True, text=True, timeout=20)
            if info.returncode:
                raise HTTPException(400, 'Downloaded file is not valid audio')
            probe = json.loads(info.stdout)
            if not probe.get('streams') or float(probe.get('format', {}).get('duration', 0)) <= 0:
                raise HTTPException(400, 'Downloaded file has no audio')
            title = re.sub(r'[^\w -]+', '', job['title'], flags=re.UNICODE).strip(' .')[:80] or 'lofi'
            dest = Path(job['output_dir']) / f'{title}_{job["id"][:10]}{source.suffix.lower()}'
            if dest.exists():
                raise HTTPException(409, 'Output already exists; refusing overwrite')
            partial = dest.with_suffix(dest.suffix + '.part')
            try:
                with source.open('rb') as src, partial.open('xb') as out:
                    shutil.copyfileobj(src, out)
                os.replace(partial, dest)
            finally:
                partial.unlink(missing_ok=True)
            # Keep the original Chrome download. No existing files are deleted.
            job.update(output=str(dest), duration=float(probe['format']['duration']), source_download=str(source))
            self.save(job)
            return {'ok': True, 'output': str(dest)}


def create_app(home: Path = HOME, *, port=7861, account=DEFAULT_ACCOUNT):
    store = MusicStore(home, port=port, account=account)
    app = FastAPI(title='LoFi Flow Music Local', docs_url=None, redoc_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])

    @app.middleware('http')
    async def local_access(request: Request, call_next):
        origin = request.headers.get('origin', '')
        local = f'http://127.0.0.1:{port}'
        if origin and origin not in (local, f'http://localhost:{port}') and not origin.startswith('chrome-extension://'):
            return JSONResponse({'detail': 'Local pages only'}, status_code=403)
        if request.url.path.startswith('/bridge/'):
            supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
            if not secrets.compare_digest(supplied, store.token):
                return JSONResponse({'detail': 'Invalid bridge token'}, status_code=403)
        elif request.url.path.startswith('/api/') and request.method != 'GET':
            if not secrets.compare_digest(request.headers.get('x-lofi-token', ''), store.token):
                return JSONResponse({'detail': 'Invalid UI token'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/', response_class=HTMLResponse)
    def index():
        return (ROOT / 'flow_music_ui.html').read_text(encoding='utf-8').replace('__LOFI_CONFIG__',
            json.dumps({'token': store.token, 'extension': str(store.extension),
                        'output': str(ROOT / 'data/musics/flowmusic')}, ensure_ascii=True))

    @app.get('/api/status')
    def status():
        return {'connected': time.time() - store.connected_at < 70, 'extension': str(store.extension),
                'jobs': store.all()[:20]}

    @app.post('/api/jobs')
    def new_job(req: NewJob):
        return store.create(req)

    @app.post('/api/cancel')
    def cancel(event: Event):
        with store.lock:
            job = store.get(event.id)
            if job['status'] not in TERMINAL:
                job['cancel_requested'] = True
                job.update(status='cancelled', message='Đã dừng bot. Yêu cầu đã gửi có thể vẫn chạy trên Flow Music.' if job['submitted'] else 'Đã hủy trước khi gửi prompt.')
                store.save(job)
            return job

    @app.post('/api/chrome/{target}')
    def chrome(target: Literal['music', 'extensions']):
        open_chrome(SITE if target == 'music' else 'chrome://extensions/', store.account)
        return {'ok': True, 'profile': profile_directory(store.account)}

    @app.get('/bridge/next')
    def next_job():
        store.connected_at = time.time()
        jobs = [job for job in store.all() if job['status'] == 'queued' and not job['cancel_requested']]
        return {'ok': True, 'job': jobs[-1] if jobs else None}

    @app.post('/bridge/claim')
    def claim(event: Event):
        with store.lock:
            job = store.get(event.id)
            if job['status'] != 'queued' or job['cancel_requested']:
                raise HTTPException(409, 'Job is already claimed or cancelled')
            job.update(status='running', message='Extension đã nhận yêu cầu.')
            store.save(job)
            return {'ok': True}

    @app.post('/bridge/state')
    def state(event: Event):
        store.connected_at = time.time()
        job = store.get(event.id)
        return {'ok': True, 'cancel_requested': job['cancel_requested'] or job['status'] in TERMINAL}

    @app.post('/bridge/progress')
    def progress(event: Event):
        with store.lock:
            job = store.get(event.id)
            if job['status'] != 'running' or event.phase not in PHASES:
                raise HTTPException(409, 'Invalid job phase')
            job.update(phase=event.phase, message=event.message)
            if event.session_url:
                if not re.fullmatch(r'https://(?:www\.)?flowmusic\.app/(?:session(?:/[^?#\s]+)?(?:\?[^#\s]*)?)', event.session_url):
                    raise HTTPException(400, 'Not a Flow Music session URL')
                job['session_url'] = event.session_url
            job['submitted'] = job['submitted'] or event.phase in ('submitting', 'submitted', 'downloading')
            store.save(job)
            return {'ok': True}

    @app.post('/bridge/download')
    def download(event: Event):
        return store.download(event)

    @app.post('/bridge/done')
    def done(event: Event):
        with store.lock:
            job = store.get(event.id)
            if not job['output']:
                raise HTTPException(409, 'No validated audio file saved')
            job.update(status='done', message='Đã lưu nhạc vào máy.')
            store.save(job)
            return {'ok': True}

    @app.post('/bridge/fail')
    def fail(event: Event):
        with store.lock:
            job = store.get(event.id)
            if job['status'] not in TERMINAL:
                job.update(status='cancelled' if job['cancel_requested'] else 'failed', message=event.message)
                store.save(job)
            return {'ok': True}

    @app.post('/bridge/diagnostic')
    def diagnostic(event: Event):
        store.get(event.id)
        (store.path(event.id).parent / 'page-text.txt').write_text(event.text, encoding='utf-8')
        return {'ok': True}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=7861)
    parser.add_argument('--home', type=Path, default=HOME)
    parser.add_argument('--account', default=DEFAULT_ACCOUNT)
    parser.add_argument('--open', action='store_true', help='Open the local tool in the selected Chrome profile')
    args = parser.parse_args()
    app = create_app(args.home, port=args.port, account=args.account)
    if args.open:
        # Chrome can open the local UI in this profile without a debugging port.
        from flow_music_browser import chrome_executable
        subprocess.Popen([chrome_executable(), '--profile-directory=' + profile_directory(args.account),
                          '--new-window', f'http://127.0.0.1:{args.port}'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
