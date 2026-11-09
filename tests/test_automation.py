import io
import json
import os
from pathlib import Path
import time
from unittest.mock import Mock
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from automation_engine import Engine
from automation_bridge import create_app, direct_workbook_download, matches_reference
from automation_telegram import Telegram
from automation_workbook import queue_records, mark_completed, read_sheets


@pytest.fixture
def workbook(tmp_path):
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    path=tmp_path/'queue.xlsx'
    with ZipFile(path,'w') as out:
        out.writestr('xl/workbook.xml','<workbook xmlns="'+ns+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{name}" sheetId="{i}" r:id="r{i}"/>' for i,name in enumerate(['Kich_ban','Prompts','YouTube'],1))+'</sheets></workbook>')
        out.writestr('xl/_rels/workbook.xml.rels','<Relationships>'+''.join(f'<Relationship Id="r{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1,4))+'</Relationships>')
        for index in range(1,4):
            root=ET.Element('worksheet',xmlns=ns); data=ET.SubElement(root,'sheetData')
            for row_id in (8,9):
                key=f'LOFI-{row_id-7:03d}'
                values={'A':key,'B':'Scene '+key,'H':'CHUA_TEST','I':False} if index==1 else (
                    {'A':key,'B':'Create image','C':'Edit previous image','D':'Animate gently','E':'Warm piano lo-fi','F':'Quiet afternoon'} if index==2 else {'A':key,'B':'YouTube title','C':'Description','D':'lofi, chill','E':'#lofi'})
                row=ET.SubElement(data,'row',r=str(row_id))
                for column,value in values.items():
                    cell=ET.SubElement(row,'c',r=f'{column}{row_id}',t='b' if isinstance(value,bool) else 'inlineStr')
                    if isinstance(value,bool): ET.SubElement(cell,'v').text='1' if value else '0'
                    else: ET.SubElement(ET.SubElement(cell,'is'),'t').text=value
            out.writestr(f'xl/worksheets/sheet{index}.xml',ET.tostring(root))
    return path


@pytest.fixture
def engine(tmp_path,monkeypatch):
    monkeypatch.setattr(Telegram,'check',lambda self:None)
    e=Engine(tmp_path/'automation')
    reference=tmp_path/'style.png'; reference.write_bytes(b'image fixture')
    style=tmp_path/'style.md'; style.write_text('Matte hand-painted grouped foliage')
    e.configure({'excel_url':'https://cmcuni-my.sharepoint.com/doc.xlsx','references':[str(reference)],
        'style_path':str(style),'remove_watermark':False,'output_dir':str(tmp_path/'outputs'),'music_count':1,
        'telegram_token':'test-token','telegram_chat_id':'123','telegram_reviewer_id':'123'})
    e.connected={'main':time.time(),'gpt':time.time()}
    return e


def finish(e,result):
    task=e.task(e.state['task_id']); e.claim(task['id'],task['role']); e.complete_task(task['id'],task['role'],result)
    return task


def test_music_downloads_25_tracks_in_same_session_before_render(engine,monkeypatch,tmp_path):
    e=engine
    e.configure({'music_count':25})
    e.state.update(record={'music_prompt':'Warm instrumental lofi'},music_index=0,artifacts={})
    rendered=[]
    monkeypatch.setattr(e,'start_render',lambda:rendered.append(True))
    e.enqueue_music()
    session='https://www.flowmusic.app/session/12345678-aaaa-bbbb-cccc-123456789abc'
    for index in range(25):
        task=e.task(e.state['task_id'])
        assert task['payload']['url']==(session if index else 'https://www.flowmusic.app/session?t=true')
        assert task['payload']['prompt']=='Warm instrumental lofi'
        assert not rendered
        e.claim(task['id'],'main')
        e.progress(task['id'],'main','downloading',True,session)
        e.complete_task(task['id'],'main',{'path':str(tmp_path/f'track{index}.mp3')})
    assert len(e.state['artifacts']['music'])==25
    assert rendered==[True]


def test_resume_renders_when_reduced_music_count_is_already_met(engine,monkeypatch):
    engine.configure({'music_count':3})
    engine.state.update(record={'music_prompt':'Warm piano'},music_index=1,
                        artifacts={'music':['first.mp3']})
    engine.enqueue_music()
    pending_id=engine.state['task_id']
    engine.pause()
    engine.configure({'music_count':1})
    rendered=Mock()
    monkeypatch.setattr(engine,'start_render',rendered)
    engine.resume()
    rendered.assert_called_once_with()
    assert engine.state['task_id']==pending_id
    assert engine.next_task('main') is None


def test_music_resume_collects_exact_song_without_sending_prompt(engine,monkeypatch):
    e=engine
    e.state.update(record={'music_prompt':'Piano'},music_index=0,artifacts={})
    e.enqueue_music()
    task=e.claim(e.state['task_id'],'main')
    song='https://www.flowmusic.app/song/third-take'
    e.progress(task['id'],'main','downloading',True,e.settings.music_session_url,song)
    e.fail_task(task['id'],'main','format menu did not open')
    e.resume()
    collect=e.task(e.state['task_id'])
    assert collect['kind']=='collect_music' and collect['payload']['song_url']==song
    rendered=[]
    monkeypatch.setattr(e,'start_render',lambda:rendered.append(True))
    finish(e,{'path':'third.mp3'})
    assert rendered and e.state['artifacts']['music']==['third.mp3']


def test_restart_from_excel_with_local_review_skips_telegram(engine,monkeypatch,tmp_path):
    e=engine
    e.state.update(run_id='previous', status='failed',message='provider failed')
    monkeypatch.setattr('automation_engine.Telegram',lambda *args:pytest.fail('unexpected Telegram access'))
    e.start(local_review=True,restart_failed=True)
    assert e.state['run_id']!='previous'
    assert json.loads((e.home/'runs/previous/state.json').read_text(encoding='utf8'))['message']=='provider failed'
    assert e.task(e.state['task_id'])['kind']=='excel_snapshot'
    e.state.update(record={'id':'LOFI-001'},output=str(tmp_path/'mix.mp4'),status='notifying')
    def preview(args,**kwargs):
        path=Path(args[-1])
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'preview')
    monkeypatch.setattr('automation_engine.subprocess.run',preview)
    e.notify()
    assert e.state['status']=='complete' and Path(e.state['review_path']).is_file()


def test_workbook_preserves_file_and_reads_native_boolean(workbook):
    before=workbook.read_bytes()
    rows=queue_records(workbook)
    assert [r['id'] for r in rows]==['LOFI-001','LOFI-002']
    assert rows[0]['checkbox_cell']=='I8'
    assert rows[0]['youtube']['title']=='YouTube title'
    assert workbook.read_bytes()==before


def test_local_checkbox_preserves_package_and_is_idempotent(workbook, tmp_path):
    before=workbook.read_bytes()
    backup=tmp_path/'backup.xlsx'
    mark_completed(workbook,'Kich_ban','LOFI-001','A8','I8',backup)
    assert backup.read_bytes()==before
    assert read_sheets(workbook)['Kich_ban'][8]['I'] is True
    assert [row['id'] for row in queue_records(workbook)]==['LOFI-002']
    with ZipFile(io.BytesIO(before)) as old, ZipFile(workbook) as new:
        changed=[name for name in old.namelist() if old.read(name)!=new.read(name)]
        assert changed==['xl/worksheets/sheet1.xml']
        assert new.read(changed[0])==old.read(changed[0]).replace(
            b'<c r="I8" t="b"><v>0</v></c>', b'<c r="I8" t="b"><v>1</v></c>')
    after=workbook.read_bytes()
    mark_completed(workbook,'Kich_ban','LOFI-001','A8','I8',backup)
    assert workbook.read_bytes()==after and backup.read_bytes()==before
    with pytest.raises(ValueError,match='row ID'):
        mark_completed(workbook,'Kich_ban','LOFI-002','A8','I8',backup)
    assert workbook.read_bytes()==after


def test_local_workbook_flow_and_retry_after_excel_lock(engine, workbook, tmp_path, monkeypatch):
    engine.configure({'excel_path':str(workbook),'excel_url':''})
    engine.start(local_review=True)
    assert engine.task(engine.state['task_id'])['role']=='local'
    assert engine.next_task('main') is None
    engine.process_local_task()
    assert engine.state['record']['id']=='LOFI-001'
    assert engine.task(engine.state['task_id'])['kind']=='generate_image'
    finish(engine,{'path':str(tmp_path/'image.png')})
    finish(engine,{'url':'https://flow.google.com/project/12345678-aaaa-bbbb-cccc-123456789abc'})
    finish(engine,{'path':str(tmp_path/'video.mp4')})
    output=tmp_path/'mix.mp4'
    output.write_bytes(b'completed media fixture')
    def render():
        engine.state.update(status='rendering',output=str(output),render_dir=str(tmp_path/'render'))
    monkeypatch.setattr(engine,'start_render',render)
    finish(engine,{'path':str(tmp_path/'music.mp3')})
    engine.worker=Mock()
    engine.worker.poll.return_value=0
    engine.worker.returncode=0
    engine.worker_log=io.BytesIO()
    monkeypatch.setattr('studio_pipeline.read_state',lambda folder:{'status':'done'})
    engine.tick_render()
    assert engine.task(engine.state['task_id'])['role']=='local'
    lock=workbook.with_name('~$'+workbook.name)
    lock.touch()
    engine.process_local_task()
    assert engine.state['status']=='failed'
    assert read_sheets(workbook)['Kich_ban'][8]['I'] is False
    lock.unlink()
    # Changing the configured path cannot redirect a pending completion write.
    engine.configure({'excel_path':str(tmp_path/'different.xlsx')})
    engine.resume()
    engine.process_local_task()
    assert engine.state['status']=='complete'
    assert read_sheets(workbook)['Kich_ban'][8]['I'] is True


def test_direct_excel_download_accepts_only_fresh_named_workbook(tmp_path,monkeypatch):
    from automation_engine import now
    monkeypatch.setattr('flow_music_browser.download_directory',lambda profile:tmp_path)
    task={'kind':'excel_snapshot','created_at':now(),'payload':{
        'url':'https://cmcuni-my.sharepoint.com/_layouts/15/Doc.aspx?sourcedoc=id&file=Run.xlsx'}}
    source=tmp_path/'Run (1).xlsx'
    source.write_bytes(b'workbook')
    os.utime(source,(time.time()+1,)*2)
    assert direct_workbook_download(task,source,'Profile 20')
    os.utime(source,(0,0))
    assert not direct_workbook_download(task,source,'Profile 20')
    assert not direct_workbook_download(task,tmp_path/'Other.xlsx','Profile 20')
    assert not direct_workbook_download({**task,'kind':'flow_music'},source,'Profile 20')


def test_resized_reference_cannot_become_generated_asset(tmp_path):
    import cv2
    import numpy as np
    original=np.zeros((96,144,3),dtype=np.uint8)
    original[:48]=[100,20,200]
    ref=tmp_path/'ref.png'
    assert cv2.imwrite(str(ref),original)
    assert matches_reference(cv2.resize(original,(72,48)),[str(ref)])
    assert not matches_reference(cv2.flip(original,0),[str(ref)])


def test_cleanup_runs_after_tick_and_keeps_final_mix_and_unrelated_files(engine, tmp_path):
    engine.state.update(run_id='a'*12, record={'id':'LOFI-001'})
    scene=engine.scenario_dir()
    prepared=scene/'media-cache'/'prepared'/'clip.mp4'
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b'prepared')
    export=tmp_path/f'LOFI-001_{engine.state["run_id"]}'
    export.mkdir()
    mix=export/'mix.mp4'
    unrelated=export/'notes.txt'
    unrelated.write_text('keep this')
    task=engine.enqueue('flow_video',{'url':'https://flow.google.com/'})
    asset=engine.home/'assets'/task['id']/'asset.mp4'
    download=tmp_path/'LoFiAutomation'/task['id']/asset.name
    for path in (asset,download,export/'video.mp4'):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'generated video')
    task['download_source']=str(download)
    engine.put_task(task)
    engine.state.update(artifacts={'video':str(asset)},output=str(mix))
    engine.enqueue('excel_tick',{})
    assert asset.is_file() and download.is_file()
    finish(engine,{'checked':True})
    assert engine.state['cleanup_pending'] and asset.is_file()
    mix.write_bytes(b'final video')
    engine.cleanup_materials()
    assert not engine.state.get('cleanup_pending')
    assert not asset.exists() and not download.exists() and not prepared.exists()
    assert set(export.iterdir())=={mix,unrelated}


def test_full_scenario_stops_until_correct_telegram_approval(engine,workbook,tmp_path,monkeypatch):
    e=engine;e.start()
    finish(e,{'path':str(workbook)})
    image=e.task(e.state['task_id'])
    assert image['role']=='gpt' and image['kind']=='generate_image'
    assert 'prompt2' not in image['payload']
    finish(e,{'path':str(tmp_path/'image.png')})
    assert e.task(e.state['task_id'])['kind']=='flow_project'
    project='https://flow.google.com/project/12345678-aaaa-bbbb-cccc-123456789abc'
    finish(e,{'url':project})
    video=e.task(e.state['task_id'])
    assert video['payload']['url']==project
    assert video['payload']['count']==1 and video['payload']['duration']==10
    assert video['payload']['model']=='Omni 1.1 Flash'
    finish(e,{'path':str(tmp_path/'native.mp4')})
    output=tmp_path/'final.mp4';output.write_bytes(b'rendered output fixture')
    def render():
        e.state.update(status='rendering',output=str(output),render_dir=str(tmp_path/'render'))
    monkeypatch.setattr(e,'start_render',render)
    finish(e,{'path':str(tmp_path/'track.mp3')})
    assert e.state['status']=='rendering'
    e.worker=Mock();e.worker.poll.return_value=0;e.worker.returncode=0;e.worker_log=io.BytesIO()
    monkeypatch.setattr('studio_pipeline.read_state',lambda folder:{'status':'done'})
    e.tick_render()
    tick=e.task(e.state['task_id'])
    assert tick['kind']=='excel_tick' and tick['payload']['id']=='LOFI-001'
    assert tick['payload']['cell']=='I8'
    e.claim(tick['id'],'main')
    with pytest.raises(ValueError,match='not verified'):e.complete_task(tick['id'],'main',{'checked':False})
    assert e.state['status']=='running'
    e.complete_task(tick['id'],'main',{'checked':True})
    assert e.state['status']=='complete'
    assert e.next_task('main') is None and e.next_task('gpt') is None
    assert e.state['completed']==['LOFI-001']


def test_flow_retry_reuses_created_project_and_migrates_unsubmitted_old_task(engine):
    e=engine
    e.state.update(record={'id':'LOFI-001','title':'Shop','animation_prompt':'Animate'},artifacts={'image':'new.png'})
    old=e.enqueue('flow_video',{'url':e.settings.flow_url})
    e.claim(old['id'],'main')
    e.fail_task(old['id'],'main','Project not found')
    e.resume()
    project=e.task(e.state['task_id'])
    assert project['kind']=='flow_project'
    e.claim(project['id'],'main')
    url='https://flow.google.com/project/12345678-aaaa-bbbb-cccc-123456789abc'
    e.progress(project['id'],'main','created',True,url)
    e.fail_task(project['id'],'main','connection lost before result')
    e.resume()
    video=e.task(e.state['task_id'])
    assert video['kind']=='flow_video' and video['payload']['url']==url
    e.claim(video['id'],'main')
    e.fail_task(video['id'],'main','settings failed')
    e.resume()
    assert e.task(e.state['task_id'])['payload']['url']==url


def test_restart_does_not_requeue_submitted_generations(engine,workbook):
    e=engine;e.start();finish(e,{'path':str(workbook)})
    task=e.task(e.state['task_id']);e.claim(task['id'],'gpt')
    e.progress(task['id'],'gpt','Image submitted',True,'https://chatgpt.com/c/test')
    restored=Engine(e.home);restored.connected={'main':time.time(),'gpt':time.time()}
    assert restored.state['status']=='interrupted'
    assert restored.task(task['id'])['status']=='interrupted'
    with pytest.raises(ValueError,match='đã gửi'):restored.resume()
    assert restored.next_task('gpt') is None
    restored.resume(allow_resubmit=True)
    assert restored.task(restored.state['task_id'])['status']=='queued'


def test_missing_chat_turn_recovers_recorded_project_conversation(engine,workbook):
    project='https://chatgpt.com/g/g-p-fixture'
    engine.configure({'gpt_project_url':project+'/project'})
    engine.start()
    finish(engine,{'path':str(workbook)})
    task=engine.claim(engine.state['task_id'],'gpt')
    engine.progress(task['id'],'gpt','sent',True,project+'/project')
    chat='d0123456-aaaa-bbbb-cccc-0123456789ab'
    (engine.tasks_dir/(task['id']+'.txt')).write_text(json.dumps({'path':f'/g/g-p-fixture/c/{chat}'}))
    engine.fail_task(task['id'],'gpt','turn selector timed out')
    recovery=engine.task(engine.state['task_id'])
    assert recovery['kind']=='collect_image' and recovery['payload']['url']=='https://chatgpt.com/c/'+chat


def test_profile_isolation_and_no_tick_without_saved_render(tmp_path):
    app=create_app(tmp_path/'bridge',start_loop=False)
    with TestClient(app,base_url='http://127.0.0.1:7861') as client:
        assert client.get('/bridge/next').status_code==403
        assert client.post('/api/start').status_code==403
        assert client.get('/api/state',headers={'Origin':'https://evil.example'}).status_code==403
        e=app.state.engine;e.state['status']='running';e.enqueue('generate_image',{'url':'https://chatgpt.com/'})
        key=app.state.keys
        main={'Authorization':'Bearer '+key['main'],'X-LoFi-Instance':'a'*32};gpt={'Authorization':'Bearer '+key['gpt'],'X-LoFi-Instance':'b'*32}
        assert client.get('/bridge/next',headers=main).json()['task'] is None
        assert client.get('/bridge/next',headers=gpt).json()['task'] is None
        gpt['X-LoFi-Single-Image']='3'
        task=client.get('/bridge/next',headers=gpt).json()['task']
        assert client.post('/bridge/claim',headers=main,json={'id':task['id']}).status_code==400
        assert client.post('/bridge/claim',headers=gpt,json={'id':task['id']}).status_code==200
        assert client.post('/bridge/claim',headers=gpt,json={'id':task['id']}).status_code==400
        assert client.post('/bridge/result',headers=gpt,json={'id':task['id'],'ok':True}).status_code==400
        assert 'telegram_token' not in client.get('/api/state').text or client.get('/api/state').json()['settings']['telegram_token']==''


def test_telegram_transport_redacts_token(monkeypatch):
    import requests
    def failure(*args,**kwargs):raise requests.ConnectionError('https://api.telegram.org/botTOPSECRET/sendMessage')
    monkeypatch.setattr(requests,'post',failure)
    with pytest.raises(RuntimeError) as error:Telegram('TOPSECRET','123').call('sendMessage')
    assert 'TOPSECRET' not in str(error.value)


def test_replacement_profile_never_steals_tasks_or_reactivates_old_account(tmp_path):
    app=create_app(tmp_path/'bridge',start_loop=False)
    with TestClient(app,base_url='http://127.0.0.1:7861') as client:
        e=app.state.engine
        old={'Authorization':'Bearer '+app.state.keys['main'],'X-LoFi-Instance':'1'*32}
        new={'Authorization':'Bearer '+app.state.keys['main'],'X-LoFi-Instance':'2'*32}
        ui={'X-LoFi-Token':app.state.keys['ui']}
        assert client.get('/bridge/profile',headers=old).json()['active']
        assert not client.get('/bridge/profile',headers=new).json()['active']
        first=e.enqueue('flow_music',{'url':'https://www.flowmusic.app/session?t=true'})
        assert client.get('/bridge/next',headers=new).json()['task'] is None
        assert client.post('/bridge/activate',headers=new,json={}).status_code==400
        assert client.post('/bridge/claim',headers=old,json={'id':first['id']}).status_code==200
        e.pause()
        assert client.post('/api/settings',headers=ui,json={'main_profile':'Profile 90'}).status_code==200
        assert not client.get('/bridge/next',headers=old).json()['active']
        assert client.post('/bridge/activate',headers=new,json={}).status_code==200
        second=e.enqueue('flow_music',{'url':'https://www.flowmusic.app/session?t=true'})
        assert client.get('/bridge/next',headers=old).json()['task'] is None
        assert client.post('/bridge/claim',headers=old,json={'id':second['id']}).status_code==403
        assert client.get('/bridge/next',headers=new).json()['task']['id']==second['id']
        assert client.post('/bridge/claim',headers=new,json={'id':second['id']}).status_code==200
    restored=create_app(tmp_path/'bridge',start_loop=False)
    assert restored.state.bindings.data['active']['main']=='2'*32


def test_switching_an_idle_profile_preserves_pending_telegram_approval(tmp_path):
    app=create_app(tmp_path/'bridge',start_loop=False)
    with TestClient(app,base_url='http://127.0.0.1:7861') as client:
        e=app.state.engine
        for identifier in ('3'*32,'4'*32):
            headers={'Authorization':'Bearer '+app.state.keys['gpt'],'X-LoFi-Instance':identifier}
            assert client.get('/bridge/profile',headers=headers).status_code==200
        e.state.update(status='awaiting_approval',approval_nonce='keep-this-nonce',telegram_message_id=77)
        new={'Authorization':'Bearer '+app.state.keys['gpt'],'X-LoFi-Instance':'4'*32}
        assert client.post('/bridge/activate',headers=new,json={}).status_code==200
        assert e.state['status']=='awaiting_approval'
        assert e.state['approval_nonce']=='keep-this-nonce'
        assert e.state['telegram_message_id']==77
        ui={'X-LoFi-Token':app.state.keys['ui']}
        assert client.post('/api/settings',headers=ui,json={'gpt_profile':'Profile 88'}).status_code==200
        assert app.state.bindings.data['active']['gpt'] is None
        assert client.post('/api/settings',headers=ui,json={'telegram_chat_id':'999'}).status_code==400
