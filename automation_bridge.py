"""Loopback-only browser bridge embedded by the Studio automation tab."""
from __future__ import annotations
import argparse
import base64
import hashlib
from contextlib import asynccontextmanager
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from automation_engine import Engine, ROOT
from automation_workbook import queue_records
from automation_profiles import ProfileBindings


class StartOptions(BaseModel):
    local_review: bool = False
    restart_failed: bool = False


def direct_workbook_download(task, path, profile):
    # The extension reports the filename of the exact chrome.downloads.download
    # ID it started for this task. Some Chrome installations override its name.
    if task['kind']!='excel_snapshot' or path.suffix.lower()!='.xlsx':
        return False
    from flow_music_browser import download_directory
    url=urlparse(task['payload']['url'])
    query=parse_qs(url.query)
    filename=query.get('file',[''])[0]
    if (url.hostname!='cmcuni-my.sharepoint.com' or not url.path.endswith('/_layouts/15/Doc.aspx')
            or not query.get('sourcedoc') or Path(filename).suffix.lower()!='.xlsx'):
        return False
    return (path.parent==download_directory(profile)
            and bool(re.fullmatch(re.escape(Path(filename).stem)+r'(?: \(\d+\))?\.xlsx', path.name, re.I))
            and path.stat().st_mtime >= datetime.fromisoformat(task['created_at']).timestamp())


def matches_reference(image, references):
    import cv2
    size=(48,48)
    thumbnail=cv2.resize(image,size,interpolation=cv2.INTER_AREA)
    for reference in references:
        original=cv2.imread(reference)
        if original is None:
            continue
        reference_thumb=cv2.resize(original,size,interpolation=cv2.INTER_AREA)
        if cv2.absdiff(thumbnail,reference_thumb).mean()<=2:
            return True
    return False


def create_app(home=ROOT/'cache/automation', port=7861, start_loop=True):
    engine=Engine(Path(home))
    bindings=ProfileBindings(engine)
    credentials=engine.home/'bridge-keys.json'
    if not credentials.exists(): credentials.write_text(json.dumps({r:secrets.token_urlsafe(32) for r in ('ui','main','gpt')}),encoding='utf-8')
    keys=json.loads(credentials.read_text(encoding='utf-8'))
    source=ROOT/'browser_extensions/lofi_automation'
    digest=hashlib.sha256()
    for path in [*sorted(source.iterdir()),ROOT/'browser_extensions/flow_music/content.js']:
        if path.is_file():
            digest.update(path.read_bytes())
    revision=digest.hexdigest()[:16]
    for role in ('main','gpt'):
        dest=engine.home/'extensions'/role; dest.mkdir(parents=True,exist_ok=True)
        for path in source.iterdir():
            if path.is_file() and path.name!='manifest.json': shutil.copyfile(path,dest/path.name)
        shutil.copyfile(ROOT/'browser_extensions/flow_music/content.js',dest/'music.js')
        manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
        hosts=['https://chatgpt.com/*'] if role=='gpt' else [
            'https://www.flowmusic.app/*','https://flowmusic.app/*','https://flow.google.com/*',
            'https://cmcuni-my.sharepoint.com/*','https://*.officeapps.live.com/*']
        manifest.update(name=f'LoFi Studio Automation · {role}')
        manifest['host_permissions']=hosts+['http://127.0.0.1/*']
        manifest['content_scripts'][0]['matches']=hosts
        (dest/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        (dest/'config.js').write_text('const LOFI_CONFIG = '+json.dumps({'role':role,'url':f'http://127.0.0.1:{port}',
            'studio_url':'http://127.0.0.1:7860','token':keys[role],'revision':revision})+';\n',encoding='utf-8')

    @asynccontextmanager
    async def lifespan(app):
        if start_loop: engine.start_loop()
        yield
        engine.stop_event.set()
    app=FastAPI(title='LoFi Studio Automation',lifespan=lifespan,docs_url=None,redoc_url=None)
    app.state.engine,app.state.keys,app.state.bindings=engine,keys,bindings
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1'])

    @app.middleware('http')
    async def guard(request, call_next):
        origin=request.headers.get('origin','')
        if origin and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}') and not origin.startswith('chrome-extension://'):
            return JSONResponse({'detail':'Local requests only'},status_code=403)
        supplied=request.headers.get('authorization','').removeprefix('Bearer ')
        request.state.role=next((r for r in ('main','gpt') if secrets.compare_digest(supplied,keys[r])),None)
        if request.url.path.startswith('/bridge/') and not request.state.role:
            return JSONResponse({'detail':'Bridge authentication required'},status_code=403)
        if request.url.path.startswith('/bridge/'):
            try:request.state.instance=bindings.register(request.state.role,request.headers.get('x-lofi-instance',''))
            except ValueError as exc:return JSONResponse({'detail':str(exc)},status_code=403)
            if bindings.selected(request.state.role,request.state.instance):engine.connected[request.state.role]=time.time()
            if request.url.path not in ('/bridge/next','/bridge/profile','/bridge/activate','/bridge/rename') and not bindings.selected(request.state.role,request.state.instance):
                return JSONResponse({'detail':'Profile này không được chọn; không giao tác vụ cho tài khoản cũ'},status_code=403)
        if request.url.path.startswith('/api/') and request.method!='GET' and not secrets.compare_digest(request.headers.get('x-lofi-token',''),keys['ui']):
            return JSONResponse({'detail':'UI authentication required'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'; response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.exception_handler(ValueError)
    async def validation_error(request,error): return JSONResponse({'detail':str(error)},status_code=400)

    @app.get('/',response_class=HTMLResponse)
    def index():
        return (ROOT/'automation_ui.html').read_text(encoding='utf-8').replace('__LOFI_CONFIG__',
            json.dumps({'token':keys['ui'],'home':str(engine.home)},ensure_ascii=True))

    @app.get('/api/state')
    def state():
        result=engine.public();result['browser_profiles']=bindings.public();return result

    @app.get('/api/profiles')
    def profiles():
        from flow_music_browser import chrome_profiles
        return {'profiles':chrome_profiles()}

    @app.post('/api/settings')
    async def settings(request: Request):
        data=await request.json()
        with engine.lock:
            changed=[role for role in ('main','gpt') if data.get(role+'_profile',getattr(engine.settings,role+'_profile'))!=getattr(engine.settings,role+'_profile')]
            for role in changed:bindings.mutable(role)
            engine.configure(data)
            for role in changed:bindings.clear(role)
        return {'ok':True}

    @app.post('/api/profiles/activate')
    async def select_instance(request: Request):
        data=await request.json()
        if data.get('role') not in ('main','gpt'):raise ValueError('Unknown role')
        bindings.activate(data['role'],data['instance']);return {'ok':True}

    @app.post('/api/start')
    def start(options: StartOptions = StartOptions()):
        engine.start(options.local_review, options.restart_failed)
        return {'ok':True}

    @app.post('/api/continue-image')
    async def continue_image(request: Request):
        body=await request.json()
        engine.continue_image(str(body.get('url','')))
        return {'ok':True}

    @app.post('/api/collect-video')
    def collect_video(): engine.collect_video(); return {'ok':True}

    @app.post('/api/pause')
    def pause(): engine.pause(); return {'ok':True}

    @app.post('/api/resume')
    async def resume(request: Request):
        data=await request.json()
        engine.resume(bool(data.get('allow_resubmit')), str(data.get('song_url') or '').strip())
        return {'ok':True}

    @app.post('/api/open/{role}')
    async def open_profile(role: str,request: Request):
        if role not in ('main','gpt'): raise ValueError('Unknown profile role')
        from flow_music_browser import chrome_executable
        body=await request.json()
        chosen=body.get('profile')
        if chosen and chosen!=getattr(engine.settings,role+'_profile'):
            with engine.lock:
                bindings.mutable(role);engine.configure({role+'_profile':chosen});bindings.clear(role)
        profile=engine.settings.main_profile if role=='main' else engine.settings.gpt_profile
        subprocess.Popen([chrome_executable(),'--profile-directory='+profile,'--new-window','chrome://extensions/'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'ok':True,'folder':str(engine.home/'extensions'/role)}

    @app.get('/api/review')
    def review():
        if not engine.state.get('record'): raise ValueError('No review available')
        path=engine.scenario_dir()/'review.mp4'
        if not path.is_file(): raise ValueError('Preview not ready')
        return FileResponse(path,media_type='video/mp4')

    @app.get('/bridge/next')
    def next_task(request: Request):
        selected=bindings.selected(request.state.role,request.state.instance)
        task=engine.next_task(request.state.role) if selected else None
        if task and task['kind'] in ('generate_image','collect_image') and request.headers.get('x-lofi-single-image')!='3':
            task=None
        return {'ok':True,'active':selected,'task':task,'revision':revision}

    @app.get('/bridge/profile')
    def own_profile(request: Request):
        role=request.state.role;instance=request.state.instance
        return {'ok':True,'role':role,'active':bindings.selected(role,instance),
                'instance':bindings.data['instances'][role][instance]}

    @app.post('/bridge/activate')
    def activate_profile(request: Request):
        bindings.activate(request.state.role,request.state.instance);return {'ok':True}

    @app.post('/bridge/rename')
    async def rename_profile(request: Request):
        body=await request.json();bindings.rename(request.state.role,request.state.instance,str(body.get('label','')));return {'ok':True}

    @app.post('/bridge/claim')
    async def claim(request: Request):
        body=await request.json(); return {'ok':True,'task':engine.claim(body['id'],request.state.role)}

    def owned(task_id,role):
        task=engine.task(task_id)
        if task['role']!=role or task['status']!='running' or engine.state.get('task_id')!=task_id or engine.state['status']!='running':
            raise ValueError('Task is not active in this profile')
        return task

    @app.post('/bridge/state')
    async def task_state(request: Request):
        body=await request.json(); task=engine.task(body['id'])
        if task['role']!=request.state.role: raise ValueError('Wrong profile')
        engine.connected[request.state.role]=__import__('time').time()
        return {'ok':True,'cancel_requested':task['status']!='running' or engine.state['status']!='running'}

    @app.post('/bridge/progress')
    async def progress(request: Request):
        b=await request.json()
        engine.progress(b['id'],request.state.role,str(b.get('message',''))[:1000],bool(b.get('submitted')),
                        str(b.get('session_url',''))[:2000], str(b.get('song_url') or '')[:2000]); return {'ok':True}

    @app.get('/bridge/assets/{task_id}')
    def assets(task_id: str,request: Request):
        task=owned(task_id,request.state.role)
        paths=task['payload'].get('assets') or ([task['payload']['image']] if task['kind']=='flow_video' else [])
        result=[]
        for value in paths:
            path=Path(value)
            if not path.is_file() or path.stat().st_size>25_000_000: raise ValueError('Missing asset or asset exceeds 25 MB')
            result.append({'name':path.name,'mime':mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                           'base64':base64.b64encode(path.read_bytes()).decode('ascii')})
        return {'ok':True,'assets':result}

    @app.post('/bridge/image-file')
    async def image_file(request: Request):
        import cv2
        import numpy as np
        body=await request.json()
        encoded=body.get('image','')
        if not isinstance(encoded,str) or len(encoded)>32_000_000:
            raise ValueError('Ảnh vượt giới hạn truyền sang Flow.')
        raw=base64.b64decode(encoded,validate=True)
        image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n') or image is None:
            raise ValueError('Ảnh ChatGPT chưa đọc được.')
        with engine.lock:
            task=owned(body['id'],request.state.role)
            if task['kind'] not in ('generate_image','collect_image'):
                raise ValueError('Sai bước nhận ảnh.')
            if matches_reference(image,engine.settings.references):
                return {'ok':True,'reference':True}
            dest=engine.home/'assets'/task['id']/'asset.png'
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(raw)
            task['artifact']=str(dest)
            engine.put_task(task)
        return {'ok':True,'path':str(dest)}

    @app.post('/bridge/download')
    async def download(request: Request):
        b=await request.json(); task=owned(b['id'],request.state.role)
        path=Path(b['source']).resolve()
        named=path.parent.name==task['id'] and path.parent.parent.name=='LoFiAutomation'
        if not path.is_file() or not (named or direct_workbook_download(task,path,engine.settings.main_profile)):
            raise ValueError('Not this task\'s newly named download')
        allowed={'excel_snapshot':{'.xlsx'},'generate_image':{'.png','.jpg','.jpeg','.webp'},'collect_image':{'.png','.jpg','.jpeg','.webp'},'flow_video':{'.mp4'},'collect_video':{'.mp4'},'flow_music':{'.mp3','.wav','.m4a','.flac'}}
        kind='flow_music' if task['kind']=='collect_music' else task['kind']
        if path.suffix.lower() not in allowed.get(kind,set()): raise ValueError('Unexpected download format')
        if task['kind']=='excel_snapshot': queue_records(path)
        elif task['kind'] in ('generate_image','collect_image'):
            import cv2
            image=cv2.imread(str(path))
            if image is None: raise ValueError('Downloaded image did not decode')
            if matches_reference(image,engine.settings.references):
                return {'ok':True,'reference':True}
        else:
            from fallin_preprocess import media_info
            if task['kind'] in ('flow_video','collect_video'): media_info(path)
            else:
                p=subprocess.run(['ffprobe','-v','error','-select_streams','a:0','-show_entries','stream=codec_type','-of','json',str(path)],capture_output=True,text=True,timeout=20)
                if p.returncode or not json.loads(p.stdout).get('streams'): raise ValueError('Downloaded track has no audio')
        folder=engine.home/'assets'/task['id']; folder.mkdir(parents=True,exist_ok=True)
        dest=folder/('asset'+path.suffix.lower())
        if dest.exists(): raise ValueError('Artifact already exists; refusing overwrite')
        with path.open('rb') as src,dest.open('xb') as out: shutil.copyfileobj(src,out)
        task['artifact']=str(dest); engine.put_task(task)
        return {'ok':True,'path':str(dest)}

    @app.post('/bridge/result')
    async def result(request: Request):
        b=await request.json(); task=owned(b['id'],request.state.role)
        if b.get('ok'):
            value=b.get('result') or {}
            if task['kind']!='excel_tick':
                if not task.get('artifact'):
                    message='No validated artifact saved'
                    if task['kind'] in ('generate_image','collect_image'):
                        message='Chưa nhận được ảnh mới khác ảnh tham chiếu. Kiểm tra phiên bản extension GPT.'
                    engine.fail_task(task['id'],request.state.role,message)
                    raise ValueError(message)
                value={'path':task['artifact']}
            try: engine.complete_task(task['id'],request.state.role,value)
            except Exception as exc:
                with engine.lock: engine.fail(str(exc))
                raise ValueError(str(exc)) from None
        else: engine.fail_task(task['id'],request.state.role,str(b.get('error','Browser task failed'))[:2000])
        return {'ok':True}

    @app.post('/bridge/diagnostic')
    async def diagnostic(request: Request):
        b=await request.json(); task=owned(b['id'],request.state.role)
        (engine.tasks_dir/(task['id']+'.txt')).write_text(str(b.get('text',''))[:18000],encoding='utf-8')
        return {'ok':True}
    return app


def ensure_running(port=7861):
    """Start only our loopback helper; never replace an unrelated listener."""
    import urllib.request
    import urllib.error
    import sys
    url=f'http://127.0.0.1:{port}/api/state'
    try:
        with urllib.request.urlopen(url,timeout=1) as response:
            if 'connected' in json.load(response): return None
    except urllib.error.HTTPError:
        raise RuntimeError(f'Port {port} belongs to another tool. Stop the old Flow Music server first.') from None
    except (OSError,ValueError): pass
    home=ROOT/'cache/automation'; home.mkdir(parents=True,exist_ok=True)
    temp=home/'tmp'; temp.mkdir(exist_ok=True)
    with (home/'bridge.log').open('ab') as log:
        process=subprocess.Popen([sys.executable,str(ROOT/'automation_bridge.py'),'--port',str(port)],cwd=ROOT,
            env={**os.environ,'TEMP':str(temp),'TMP':str(temp),'TMPDIR':str(temp)},stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    end=time.time()+20
    while time.time()<end:
        if process.poll() is not None: raise RuntimeError('Browser bridge failed to start. See cache/automation/bridge.log')
        try:
            with urllib.request.urlopen(url,timeout=1) as response:
                if 'connected' in json.load(response): return process
        except (OSError,ValueError): pass
        time.sleep(.2)
    raise RuntimeError('Browser bridge startup timed out')


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=7861); parser.add_argument('--home',type=Path,default=ROOT/'cache/automation')
    args=parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.home,args.port),host='127.0.0.1',port=args.port,log_level='warning')
