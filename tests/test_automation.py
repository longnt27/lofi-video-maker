import io
import json
from pathlib import Path
import time
from unittest.mock import Mock
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from automation_engine import Engine
from automation_bridge import create_app
from automation_telegram import Telegram
from automation_workbook import queue_records


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
        'style_path':str(style),'remove_watermark':False,'output_dir':str(tmp_path/'outputs'),
        'telegram_token':'test-token','telegram_chat_id':'123','telegram_reviewer_id':'123'})
    e.connected={'main':time.time(),'gpt':time.time()}
    return e


def finish(e,result):
    task=e.task(e.state['task_id']); e.claim(task['id'],task['role']); e.complete_task(task['id'],task['role'],result)
    return task


def test_workbook_preserves_file_and_reads_native_boolean(workbook):
    before=workbook.read_bytes()
    rows=queue_records(workbook)
    assert [r['id'] for r in rows]==['LOFI-001','LOFI-002']
    assert rows[0]['checkbox_cell']=='I8'
    assert rows[0]['youtube']['title']=='YouTube title'
    assert workbook.read_bytes()==before


def test_full_scenario_stops_until_correct_telegram_approval(engine,workbook,tmp_path,monkeypatch):
    e=engine;e.start()
    finish(e,{'path':str(workbook)})
    image=e.task(e.state['task_id'])
    assert image['role']=='gpt' and image['kind']=='image_pair'
    assert image['payload']['prompt2']=='Edit previous image'
    finish(e,{'path':str(tmp_path/'image.png')})
    video=e.task(e.state['task_id'])
    assert video['payload']['count']==1 and video['payload']['duration']==10
    assert video['payload']['model']=='Omni Flash'
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
    assert e.state['status']=='notifying'
    bot=Mock();bot.notify.return_value={'message_id':77}
    bot.allowed_callback.side_effect=lambda cb,message:Telegram('test','123').allowed_callback(cb,message)
    monkeypatch.setattr('automation_engine.Telegram',lambda *args:bot)
    monkeypatch.setattr('automation_engine.subprocess.run',lambda *args,**kwargs:None)
    e.notify();assert e.state['status']=='awaiting_approval'
    assert e.next_task('main') is None and e.next_task('gpt') is None
    callback={'id':'callback','from':{'id':123},'message':{'chat':{'id':123},'message_id':77},
              'data':f'lf:{e.state["run_id"]}:{e.state["approval_nonce"]}:approve'}
    assert not e.handle_callback({**callback,'from':{'id':999}})
    assert not e.handle_callback({**callback,'data':'lf:old:stale:approve'})
    assert e.handle_callback(callback)
    assert not e.handle_callback(callback)
    assert e.state['completed']==['LOFI-001']
    finish(e,{'path':str(workbook)})
    assert e.state['record']['id']=='LOFI-002'  # Even a stale snapshot cannot rerun the approved item.


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


def test_profile_isolation_and_no_tick_without_saved_render(tmp_path):
    app=create_app(tmp_path/'bridge',start_loop=False)
    with TestClient(app,base_url='http://127.0.0.1:7861') as client:
        assert client.get('/bridge/next').status_code==403
        assert client.post('/api/start').status_code==403
        assert client.get('/api/state',headers={'Origin':'https://evil.example'}).status_code==403
        e=app.state.engine;e.state['status']='running';e.enqueue('image_pair',{'url':'https://chatgpt.com/'})
        key=app.state.keys
        main={'Authorization':'Bearer '+key['main']};gpt={'Authorization':'Bearer '+key['gpt']}
        assert client.get('/bridge/next',headers=main).json()['task'] is None
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
