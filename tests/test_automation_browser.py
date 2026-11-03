"""Actual Chrome DOM checks for safe Excel edits and two image passes."""
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from flow_music_browser import ROOT, chrome_executable


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    try: executable=chrome_executable()
    except FileNotFoundError: pytest.skip('Chrome not installed')
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=executable,headless=True)
        yield browser.new_page()
        browser.close()


def scripts(page):
    page.evaluate('window.__LOFI_PROVIDER_TEST__=true')
    for name in ('shared.js','providers.js'):
        page.add_script_tag(path=str(ROOT/'browser_extensions/lofi_automation'/name))
    page.evaluate('lofi.pause=async()=>new Promise(r=>setTimeout(r,5))')


def test_excel_checks_id_selects_one_cell_and_ticks_only_if_false(page):
    page.set_content('''<button role="tab">Kich_ban</button><input aria-label="Name Box">
        <input aria-label="Formula Bar"><span role="status" aria-label="Saved to OneDrive"></span>''')
    page.evaluate('''() => {
      window.cells={A8:'LOFI-001',I8:'FALSE'};window.selected='A8';window.spaces=0;
      window.chrome={runtime:{sendMessage:async m=>{
        const box=document.querySelector('[aria-label="Name Box"]');
        if(m.type==='key'){
          if(m.key==='ControlA')box.value='';
          if(m.text!==undefined)box.value=m.text;
          if(m.key==='Enter'){selected=box.value;document.querySelector('[aria-label="Formula Bar"]').value=cells[selected];}
          if(m.key==='Space'){spaces++;cells[selected]=cells[selected]==='TRUE'?'FALSE':'TRUE';}
        }return {ok:true,cancel_requested:false};
      }}};
    }''')
    scripts(page)
    task={'id':'test','payload':{'sheet':'Kich_ban','id':'LOFI-001','id_cell':'A8','cell':'I8'}}
    assert page.evaluate('task=>lofiProviderTest.providerMap.excel_tick(task)',task)=={'checked':True}
    assert page.evaluate('spaces')==1
    assert page.evaluate('task=>lofiProviderTest.providerMap.excel_tick(task)',task)=={'checked':True}
    assert page.evaluate('spaces')==1  # A retry cannot untick an already-checked box.
    page.evaluate("cells.A8='LOFI-999'")
    with pytest.raises(Exception,match='row ID does not match'):
        page.evaluate('task=>lofiProviderTest.providerMap.excel_tick(task)',task)
    assert page.evaluate('spaces')==1


def test_image_passes_share_chat_and_download_only_second_result(page):
    from PIL import Image
    images=[]
    for color in ('red','green'):
        buf=io.BytesIO();Image.new('RGB',(480,360),color).save(buf,format='PNG')
        images.append('data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode())
    page.set_content('''<main><form><div id="prompt-textarea" contenteditable="true" role="textbox"></div>
        <input type="file" multiple><button type="button" data-testid="send-button">Send</button></form><div id="turns"></div></main>''')
    page.evaluate('''images=>{
      window.sends=[];window.messages=[];window.downloads=[];
      const composer=document.querySelector('#prompt-textarea');
      document.querySelector('[data-testid="send-button"]').onclick=()=>{
        sends.push(composer.textContent);
        const turn=document.createElement('div');turn.dataset.messageAuthorRole='assistant';
        const image=new Image();image.src=images[sends.length-1];image.alt='Generated image';turn.append(image);
        const save=document.createElement('button');save.textContent='Download image';save.onclick=()=>downloads.push(image.src);turn.append(save);document.querySelector('#turns').append(turn);
      };
      window.chrome={runtime:{sendMessage:async m=>{
        messages.push(m);
        if(m.type==='assets')return {ok:true,assets:[{name:'STYLE.png',mime:'image/png',base64:images[0].split(',')[1]}]};
        if(m.type==='key'){
          if(m.key==='ControlA')composer.textContent='';
          if(m.text!==undefined)composer.textContent=m.text;
        }
        return {ok:true,cancel_requested:false,path:'final.png'};
      }}};
    }''',images)
    scripts(page)
    task={'id':'test','payload':{'prompt1':'Create the scene','prompt2':'Edit this previous image','timeout':30}}
    result=page.evaluate('task=>lofiProviderTest.providerMap.image_pair(task)',task)
    assert result['path']=='final.png'
    assert page.evaluate('sends')==['Create the scene','Edit this previous image']
    assert page.evaluate('downloads')==[images[1]]
    assert page.locator('input[type=file]').evaluate('input=>input.files[0].name')=='STYLE.png'


def test_download_capture_requires_event_from_the_bot_tab():
    node=shutil.which('node')
    if not node:
        candidate=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        node=str(candidate) if candidate.exists() else None
    if not node: pytest.skip('Node required for extension event test')
    script=r'''
const vm=require('node:vm'),fs=require('node:fs');const handlers={};
const event=name=>({addListener:fn=>{handlers[name]=fn;}});
const chrome={downloads:{onDeterminingFilename:event('filename'),onCreated:event('created')},
 debugger:{onEvent:event('debug'),onDetach:event('detach')},
 runtime:{onMessage:event('message'),onInstalled:event('installed'),onStartup:event('startup')},
 tabs:{onRemoved:event('removed')},alarms:{onAlarm:event('alarm')}};
const context=vm.createContext({chrome,importScripts:()=>{},LOFI_CONFIG:{url:'http://127.0.0.1:7861',token:'fixture'},URL,setTimeout,AbortSignal,console});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext("active={id:'job',tab:7};watch={id:'job',url:'https://chatgpt.com/c/bot',verifiedUrls:new Set(),candidates:[]}",context);
handlers.created({id:1,url:'https://cdn.example/private-photo',referrer:'https://chatgpt.com/'});
handlers.debug({tabId:99},'Page.downloadWillBegin',{url:'https://cdn.example/private-photo'});
const before=vm.runInContext("watch.downloadId===undefined&&!match({url:'https://cdn.example/private-photo'})",context);
handlers.created({id:2,url:'https://cdn.example/bot-image',referrer:'https://chatgpt.com/'});
handlers.debug({tabId:7},'Page.downloadWillBegin',{url:'https://cdn.example/bot-image'});
const after=vm.runInContext("watch.downloadId===2&&match({url:'https://cdn.example/bot-image'})&&!match({url:'https://cdn.example/private-photo'})",context);
console.log(JSON.stringify({before,after}));
'''
    run=subprocess.run([node,'-e',script,str(ROOT/'browser_extensions/lofi_automation/background.js')],
                       capture_output=True,text=True,check=True)
    assert json.loads(run.stdout)=={'before':True,'after':True}
