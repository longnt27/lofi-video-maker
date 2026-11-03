"""Durable, one-scenario-at-a-time pipeline with a Telegram approval gate."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse
import uuid

from pydantic import BaseModel, Field

from automation_telegram import Telegram
from automation_workbook import queue_records
from flow_music_browser import ROOT


def now(): return datetime.now(timezone.utc).isoformat()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.new')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


class Settings(BaseModel):
    excel_url: str = ''
    main_profile: str = 'Profile 20'
    gpt_profile: str = 'Profile 52'
    gpt_project_url: str = ''
    flow_url: str = 'https://flow.google.com/project/5d272dab-59e3-4a29-9297-b139770754b8'
    flow_model: str = 'Omni Flash'
    references: list[str] = Field(default_factory=list)
    style_path: str = ''
    output_dir: str = str(ROOT / 'data/outputs')
    music_count: int = Field(default=1, ge=1, le=30)
    remove_watermark: bool = True
    watermark_box: list[float] | None = None  # Normalized x,y,width,height, explicitly calibrated.
    variant: str = 'soft'
    device: str = 'cuda'
    edge: int | None = None
    source_audio_volume: float = Field(default=10, ge=0, le=100)
    preview_seconds: int | None = None
    telegram_token: str = ''
    telegram_chat_id: str = ''
    telegram_reviewer_id: str = ''


class Engine:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.settings_path, self.state_path = self.home/'settings.json', self.home/'state.json'
        self.settings = Settings(**json.loads(self.settings_path.read_text(encoding='utf-8'))) if self.settings_path.exists() else Settings()
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {'status': 'idle', 'message': 'Chưa chạy', 'completed': [], 'telegram_offset': 0}
        self.connected = {}
        self.worker = None
        self.worker_log = None
        self.polling_thread = None
        self.last_telegram_error = ''
        self.tasks_dir = self.home/'tasks'
        self.tasks_dir.mkdir(exist_ok=True)
        if self.state['status'] not in ('idle', 'complete', 'awaiting_approval', 'paused', 'failed', 'interrupted'):
            self.state.update(status='interrupted', message='Đã khởi động lại. Kiểm tra bước đang dở trước khi tiếp tục.')
        for path in self.tasks_dir.glob('*.json'):
            task = json.loads(path.read_text(encoding='utf-8'))
            if task['status'] in ('queued', 'running'):
                task['status'] = 'interrupted'
                atomic(path, task)
        self.save()

    def save(self):
        self.state['updated_at'] = now()
        atomic(self.state_path, self.state)

    def configure(self, data):
        with self.lock:
            current = self.settings.model_dump()
            # A blank password field preserves the saved bot token.
            if not data.get('telegram_token'): data = {k:v for k,v in data.items() if k != 'telegram_token'}
            proposed=Settings(**{**current, **data})
            changed={key for key,value in proposed.model_dump().items() if current.get(key)!=value}
            if self.state['status'] not in ('idle','complete','paused','failed','interrupted'):
                if not changed.issubset({'main_profile','gpt_profile','gpt_project_url','flow_url'}):
                    raise ValueError('Dừng quy trình trước khi đổi cấu hình xử lý/Telegram')
                if changed & {'main_profile','flow_url'}:self.assert_profile_idle('main')
                if changed & {'gpt_profile','gpt_project_url'}:self.assert_profile_idle('gpt')
            self.settings = proposed
            atomic(self.settings_path, self.settings.model_dump())

    def assert_profile_idle(self,role):
        if self.state.get('task_id') and self.state['status']=='running':
            task=self.task(self.state['task_id'])
            if task['role']==role and task['status'] in ('queued','running'):
                raise ValueError(f'Profile {role} đang xử lý một bước. Chờ bước xong hoặc bấm Dừng trước khi chuyển.')

    def preflight(self):
        c = self.settings
        if urlparse(c.excel_url).hostname != 'cmcuni-my.sharepoint.com': raise ValueError('Cần link Excel thuộc cmcuni-my.sharepoint.com')
        if not re.fullmatch(r'https://flow\.google\.com/project/[0-9a-fA-F-]{36}/?', c.flow_url): raise ValueError('Cần URL project Google Flow')
        if not 1 <= len(c.references) <= 5 or any(not Path(p).is_file() for p in c.references): raise ValueError('Chọn 1–5 ảnh style đã duyệt trên máy')
        if c.gpt_project_url and not re.fullmatch(r'https://chatgpt\.com/g/g-p-[a-zA-Z0-9-]+/project/?',c.gpt_project_url): raise ValueError('URL project ChatGPT không hợp lệ')
        if not c.gpt_project_url and (not c.style_path or not Path(c.style_path).is_file()): raise ValueError('Chọn project ChatGPT chứa style.md hoặc file style.md trên máy')
        if c.remove_watermark:
            b = c.watermark_box
            if not b or len(b) != 4 or not all(math.isfinite(n) for n in b) or min(b[:2]) < 0 or min(b[2:]) <= 0 or b[0]+b[2] > 1 or b[1]+b[3] > 1 or b[2]*b[3] > .25:
                raise ValueError('Cần khoanh watermark một lần và lưu tọa độ chuẩn hóa x,y,w,h; không tự đoán vùng xóa')
        if c.variant not in ('soft','strong') or c.device not in ('auto','cpu','cuda'): raise ValueError('Sai cấu hình upscale')
        if c.preview_seconds is not None and c.preview_seconds < 1: raise ValueError('Thời lượng xem thử phải dương')
        bot=Telegram(c.telegram_token, c.telegram_chat_id, c.telegram_reviewer_id or None)
        for role in ('main','gpt'):
            if time.time()-self.connected.get(role, 0) > 75: raise ValueError(f'Extension profile {role} chưa kết nối')
        bot.check()
        Path(c.output_dir).mkdir(parents=True, exist_ok=True)

    def public(self):
        with self.lock:
            config = self.settings.model_dump()
            config['telegram_token'] = ''
            config['telegram_configured'] = bool(self.settings.telegram_token)
            return {'settings': config, 'state': json.loads(json.dumps(self.state)),
                    'connected': {r: time.time()-self.connected.get(r,0) < 75 for r in ('main','gpt')},
                    'telegram_error': self.last_telegram_error}

    def task(self, task_id):
        if not re.fullmatch(r'[0-9a-f]{32}', task_id): raise ValueError('Invalid task ID')
        path = self.tasks_dir/(task_id+'.json')
        if not path.exists(): raise ValueError('Task not found')
        return json.loads(path.read_text(encoding='utf-8'))

    def put_task(self, task): atomic(self.tasks_dir/(task['id']+'.json'), task)

    def enqueue(self, kind, payload):
        role = 'gpt' if kind == 'image_pair' else 'main'
        task = {'id': uuid.uuid4().hex, 'kind': kind, 'role': role, 'status': 'queued',
                'payload': payload, 'created_at': now(), 'submitted': False, 'session_url': ''}
        self.put_task(task)
        self.state.update(task_id=task['id'], status='running', stage=kind, message='Đang đợi '+kind)
        self.save()
        return task

    def next_task(self, role):
        with self.lock:
            self.connected[role] = time.time()
            if self.state['status'] not in ('running','checking_excel'): return None
            task = self.task(self.state['task_id'])
            return task if task['role'] == role and task['status'] == 'queued' else None

    def claim(self, task_id, role):
        with self.lock:
            task = self.task(task_id)
            if task['role'] != role or task['status'] != 'queued' or self.state.get('task_id') != task_id or self.state['status'] != 'running':
                raise ValueError('Task not claimable')
            task['status'] = 'running'
            self.put_task(task)
            return task

    def progress(self, task_id, role, message, submitted=False, session_url=''):
        with self.lock:
            task = self.task(task_id)
            if task['role'] != role or task['status'] != 'running' or self.state.get('task_id') != task_id: raise ValueError('Task is not running')
            if session_url and urlparse(session_url).hostname not in ('chatgpt.com','www.flowmusic.app','flowmusic.app','flow.google.com','cmcuni-my.sharepoint.com'):
                raise ValueError('Unexpected provider URL')
            task.update(submitted=task['submitted'] or submitted, session_url=session_url or task['session_url'])
            self.put_task(task)
            self.state['message'] = message
            self.save()

    def start(self):
        with self.lock:
            if self.state['status'] not in ('idle','complete'): raise ValueError('Quy trình đang chạy hoặc đang chờ duyệt; không tạo phiên mới')
            self.preflight()
            self.state = {'run_id': uuid.uuid4().hex[:12], 'status':'running', 'completed':[], 'telegram_offset':self.state.get('telegram_offset',0)}
            self.enqueue('excel_snapshot', {'url':self.settings.excel_url})

    def complete_task(self, task_id, role, result):
        with self.lock:
            task = self.task(task_id)
            if task['role'] != role or task['status'] != 'running' or self.state.get('task_id') != task_id or self.state['status'] != 'running':
                raise ValueError('Result does not belong to the active task')
            if task['kind']=='excel_tick' and result.get('checked') is not True:
                raise ValueError('Excel checkbox change was not verified')
            task.update(status='done', result=result)
            self.put_task(task)
            kind = task['kind']
            if kind == 'excel_snapshot':
                records = queue_records(Path(result['path']))
                records = [r for r in records if r['id'] not in self.state['completed']]
                if not records:
                    self.state.update(status='complete', message='Không còn kịch bản chưa tick.'); self.save(); return
                record = records[0]
                self.state.update(record=record, artifacts={}, music_index=0)
                self.scenario_dir().mkdir(parents=True, exist_ok=True)
                atomic(self.scenario_dir()/'youtube.json', record['youtube'])
                self.enqueue('image_pair', {'url':self.settings.gpt_project_url or 'https://chatgpt.com/', 'prompt1':record['image_prompt_1'],
                    'prompt2':record['image_prompt_2'], 'project':bool(self.settings.gpt_project_url),
                    'assets':self.settings.references+([self.settings.style_path] if self.settings.style_path else []), 'timeout':900})
            elif kind == 'image_pair':
                self.state['artifacts']['image'] = result['path']
                self.enqueue('flow_video', {'url':self.settings.flow_url, 'model':self.settings.flow_model,
                    'duration':10, 'count':1, 'image':result['path'], 'prompt':self.state['record']['animation_prompt'], 'timeout':1200})
            elif kind == 'flow_video':
                self.state['artifacts']['video'] = result['path']
                self.enqueue_music()
            elif kind == 'flow_music':
                self.state['artifacts'].setdefault('music',[]).append(result['path'])
                self.state['music_index'] += 1
                if self.state['music_index'] < self.settings.music_count: self.enqueue_music()
                else: self.start_render()
            elif kind == 'excel_tick':
                if result.get('checked') is not True: raise ValueError('Excel checkbox change was not verified')
                self.state.update(status='notifying', message='Đã tick Excel. Đang chuẩn bị gửi Telegram.', notification_started=False)
                self.save()

    def enqueue_music(self):
        r = self.state['record']
        self.enqueue('flow_music', {'url':'https://www.flowmusic.app/session?t=true',
            'prompt':r['music_prompt']+f'\nVariation {self.state["music_index"]+1}.',
            'instrumental':True, 'format':'mp3', 'timeout':900})

    def scenario_dir(self): return self.home/'runs'/self.state['run_id']/self.state['record']['id']

    def start_render(self):
        from fallin_preprocess import media_info
        c, a = self.settings, self.state['artifacts']
        scene = self.scenario_dir()
        music = scene/'music'; music.mkdir(exist_ok=True)
        for index,path in enumerate(a['music']):
            dest = music/f'{index+1:02d}{Path(path).suffix}'
            if not dest.exists(): shutil.copy2(path,dest)
        box = None
        if c.remove_watermark:
            info = media_info(Path(a['video']))
            x,y,w,h = c.watermark_box
            box = [round(x*info['width']), round(y*info['height']), round(w*info['width']), round(h*info['height'])]
            box[2] = min(box[2], info['width']-box[0]); box[3] = min(box[3], info['height']-box[1])
        output = Path(c.output_dir)/f'{self.state["record"]["id"]}_{self.state["run_id"]}.mp4'
        if output.exists():
            if media_info(output)['duration']<=0: raise ValueError('Existing output is invalid; refusing overwrite')
            self.state['output']=str(output)
            record=self.state['record']
            self.enqueue('excel_tick',{'url':c.excel_url,'sheet':record['sheet'],'id':record['id'],
                'id_cell':f'A{record["row"]}','cell':record['checkbox_cell']}); return
        folder = scene/'render'/uuid.uuid4().hex[:10]; folder.mkdir(parents=True)
        config = dict(kind='mix', home=str(ROOT/'cache/studio'), job_dir=str(folder), source=a['video'],
            output=str(output), box=box, variant=c.variant, device=c.device, tile=True, edge=c.edge,
            seconds=10, grain=0, fps=None, music=str(music), recursive=False, shuffle=False, seed=42,
            crossfade=2, fade=3, seamless=True, overlap=1, dust=False, preview=c.preview_seconds,
            codec='hevc', keep_source_audio=True, source_audio_volume=c.source_audio_volume)
        atomic(folder/'job.json',config)
        self.worker_log = (folder/'worker.log').open('ab')
        temp = self.home/'tmp'; temp.mkdir(exist_ok=True)
        import sys
        self.worker = subprocess.Popen([sys.executable,str(ROOT/'studio_pipeline.py'),'--job',str(folder/'job.json')],
            cwd=ROOT, env={**os.environ, 'TEMP':str(temp),'TMP':str(temp),'TMPDIR':str(temp)},
            stdout=self.worker_log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.state.update(status='rendering', stage='render', output=str(output), render_dir=str(folder),
                          message='Xóa watermark → upscale → loop và ghép nhạc trên máy.')
        self.save()

    def fail(self, message):
        self.state.update(status='failed', message=str(message)[:2000]); self.save()

    def fail_task(self, task_id, role, message):
        with self.lock:
            task=self.task(task_id)
            if task['role']!=role or task['status']!='running' or self.state.get('task_id')!=task_id: raise ValueError('Task is not active')
            task.update(status='failed', error=message); self.put_task(task)
            self.fail(message)

    def pause(self):
        with self.lock:
            was_rendering=self.state['status']=='rendering'
            if self.worker and self.worker.poll() is None:
                import psutil
                parent=psutil.Process(self.worker.pid)
                for child in parent.children(recursive=True):
                    try: child.kill()
                    except psutil.NoSuchProcess: pass
                parent.kill()
                self.worker.wait(timeout=10)
            if self.worker_log: self.worker_log.close(); self.worker_log=None
            self.worker=None
            if was_rendering and self.state.get('output'):
                from studio_pipeline import partial_output
                partial_output(Path(self.state['output'])).unlink(missing_ok=True)
            if self.state.get('task_id'):
                task=self.task(self.state['task_id'])
                if task['status'] in ('running','queued'): task['status']='cancelled'; self.put_task(task)
            self.state.update(status='paused', message='Đã dừng. Yêu cầu đã gửi lên dịch vụ có thể vẫn hoàn thành.'); self.save()

    def resume(self, allow_resubmit=False):
        with self.lock:
            if self.state['status'] not in ('paused','failed','interrupted'): raise ValueError('Không có bước đang dừng để tiếp tục')
            self.preflight()
            task=self.task(self.state['task_id']) if self.state.get('task_id') else None
            if self.state.get('telegram_message_id') and self.state.get('output'):
                # Resume still requires a fresh Telegram approval, never skips the gate.
                self.state.update(status='notifying',notification_started=False); self.save(); return
            if task and task.get('artifact') and task['status']!='done':
                task['status']='running'; self.put_task(task); self.state['status']='running'
                self.complete_task(task['id'],task['role'],{'path':task['artifact']}); return
            if task and task['status']!='done':
                if task['kind'] in ('image_pair','flow_video','flow_music') and task['submitted'] and not allow_resubmit:
                    raise ValueError('Bước này đã gửi prompt. Kiểm tra phiên trên web; chỉ cho phép gửi lại khi bạn bật xác nhận riêng.')
                payload=dict(task['payload'])
                if task['kind']=='image_pair':
                    payload.update(url=self.settings.gpt_project_url or 'https://chatgpt.com/',
                        assets=self.settings.references+([self.settings.style_path] if self.settings.style_path else []))
                elif task['kind']=='flow_video':payload.update(url=self.settings.flow_url,model=self.settings.flow_model)
                self.enqueue(task['kind'],payload); return
            if self.state.get('notification_started'):
                self.state.update(status='notifying',notification_started=False); self.save(); return
            if self.state.get('artifacts',{}).get('music'):
                self.start_render(); return
            self.enqueue('excel_snapshot',{'url':self.settings.excel_url})

    def tick_render(self):
        from studio_pipeline import read_state
        with self.lock:
            if self.state['status']!='rendering' or not self.worker: return
            info=read_state(Path(self.state['render_dir']))
            self.state['message']=info.get('stage','Đang render…')
            if self.worker.poll() is None: return
            self.worker_log.close(); self.worker_log=None
            code=self.worker.returncode; self.worker=None
            if code or info.get('status')!='done' or not Path(self.state['output']).is_file():
                self.fail(info.get('error') or 'Render thất bại. Xem worker.log.'); return
            record=self.state['record']
            self.enqueue('excel_tick', {'url':self.settings.excel_url, 'sheet':record['sheet'],
                'id':record['id'], 'id_cell':f'A{record["row"]}', 'cell':record['checkbox_cell']})

    def notify(self):
        with self.lock:
            if self.state['status']!='notifying' or self.state.get('notification_started'): return
            self.state.update(notification_started=True, approval_nonce=secrets.token_hex(6)); self.save()
            nonce=self.state['approval_nonce']; run=self.state['run_id']; record=self.state['record']; output=self.state['output']
        try:
            preview=self.scenario_dir()/'review.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-i',output,'-t','20','-vf','scale=640:-2',
                '-c:v','libx264','-preset','veryfast','-crf','25','-c:a','aac','-b:a','96k','-movflags','+faststart',str(preview)],
                check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90)
            bot=Telegram(self.settings.telegram_token,self.settings.telegram_chat_id,self.settings.telegram_reviewer_id or None)
            text=f'{record["id"]} · {record["title"]}\nĐã xuất bản mix và tick Excel.\n{output}\nApprove để chạy kịch bản tiếp theo; Dừng để giữ hàng đợi.'
            message=bot.notify(text,f'lf:{run}:{nonce}:approve',f'lf:{run}:{nonce}:stop',preview)
            with self.lock:
                if self.state['status']=='notifying':
                    self.state.update(status='awaiting_approval', telegram_message_id=message['message_id'], message='Đã gửi Telegram. Chờ bạn duyệt.'); self.save()
        except Exception as exc:
            with self.lock: self.fail(str(exc)+' Không tự gửi lại để tránh thông báo trùng.')

    def handle_callback(self, callback):
        with self.lock:
            if self.state['status']!='awaiting_approval': return False
            bot=Telegram(self.settings.telegram_token,self.settings.telegram_chat_id,self.settings.telegram_reviewer_id or None)
            if not bot.allowed_callback(callback,self.state.get('telegram_message_id')): return False
            prefix=f'lf:{self.state["run_id"]}:{self.state["approval_nonce"]}:'
            data=callback.get('data','')
            if data not in (prefix+'approve',prefix+'stop'): return False
            message_id=self.state['telegram_message_id']
            if data.endswith(':stop'):
                self.state.update(status='paused',message='Bạn đã chọn Dừng trên Telegram.'); self.save()
            else:
                self.state['completed'].append(self.state['record']['id'])
                self.enqueue('excel_snapshot',{'url':self.settings.excel_url})
        try: bot.answer(callback['id'],'Đã duyệt. Đang lấy kịch bản tiếp theo.' if data.endswith(':approve') else 'Đã dừng.'); bot.remove_buttons(message_id)
        except RuntimeError: pass  # Approval was already persisted; never replay it.
        return True

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick_render()
                self.notify()
            except Exception as exc:
                with self.lock: self.fail(str(exc))
            try:
                if self.settings.telegram_token and self.settings.telegram_chat_id:
                    bot=Telegram(self.settings.telegram_token,self.settings.telegram_chat_id,self.settings.telegram_reviewer_id or None)
                    for update in bot.updates(self.state.get('telegram_offset',0)):
                        if 'callback_query' in update: self.handle_callback(update['callback_query'])
                        with self.lock:
                            self.state['telegram_offset']=update['update_id']+1; self.save()
                    self.last_telegram_error=''
            except Exception as exc:
                self.last_telegram_error=str(exc)[:300]
            self.stop_event.wait(1)

    def start_loop(self):
        self.polling_thread=threading.Thread(target=self.loop,daemon=True); self.polling_thread.start()
