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


def test_music_collection_opens_click_submenu_and_keeps_same_title_takes_separate(page):
    page.set_content('''<article><a href="https://www.flowmusic.app/song/first">Same title</a>
        <button aria-label="Play Same title"></button><button id="old" aria-label="More options for Same title"></button></article>
        <article><a href="https://www.flowmusic.app/song/third">Same title</a>
        <button aria-label="Play Same title"></button><button id="new" aria-label="More options for Same title"></button></article>
        <button id="download" hidden>Download audio</button><button id="format" hidden>MP3</button>''')
    scripts(page)
    page.add_script_tag(path=str(ROOT/'browser_extensions/flow_music/content.js'))
    page.evaluate('''() => {
      window.events=[];
      window.chrome={runtime:{sendMessage: async m=>{events.push(m);return {ok:true}}}};
      document.querySelector('#new').onclick=()=>document.querySelector('#download').hidden=false;
      document.querySelector('#download').onclick=()=>document.querySelector('#format').hidden=false;
      document.querySelector('#format').onclick=()=>events.push({type:'mp3'});
    }''')
    song='https://www.flowmusic.app/song/third'
    page.evaluate('song=>lofiProviderTest.providerMap.collect_music({id:"job",payload:{song_url:song,format:"mp3"}})',song)
    assert page.evaluate('events.filter(x=>x.type==="progress").map(x=>x.song_url)')==[song]
    assert page.evaluate('events.map(x=>x.type).slice(-2)')==['mp3','finish-download']


def test_flow_menu_radio_options_with_icon_text_are_found(page):
    page.set_content('''<div role="menuitemradio">videocam\nVideo</div>
      <div role="menuitemradio">chrome_extension\nThành phần</div>
      <div role="menuitemradio">crop_16_9\n16:9</div>''')
    page.add_script_tag(path=str(ROOT/'browser_extensions/lofi_automation/shared.js'))
    names=page.evaluate(r'''() => [
      /^(videocam\s*)?video$/i,
      /^(chrome_extension\s*)?(ingredients|thành phần)$/i,
      /^(crop_16_9\s*)?16:9$/i
    ].map(pattern=>lofi.find(pattern)?.getAttribute('role'))''')
    assert names==['menuitemradio']*3


def test_flow_model_button_has_generic_accessibility_label(page):
    page.set_content('''<button aria-label="Chọn nhóm mô hình">Omni 1.1 Flash<span>arrow_drop_down</span></button>''')
    page.add_script_tag(path=str(ROOT/'browser_extensions/lofi_automation/shared.js'))
    assert page.evaluate("Boolean(lofi.find(/Omni 1[.]1 Flash/i))")
    assert page.evaluate("Boolean(lofi.find(/^Chọn nhóm mô hình$/i))")


@pytest.mark.parametrize("collect", [False, True])
def test_flow_reacquires_editor_after_pasting_image(page,collect):
    page.set_content('''<button>Điều kiện kích hoạt cài đặt</button>
      <button onclick="window.clickedSidebar=true">videocam Video</button>
      <button role="radio">videocam Video</button><button role="radio">chrome_extension Thành phần</button>
      <button role="radio">crop_16_9 16:9</button><button aria-label="Chọn nhóm mô hình">Omni 1.1 Flash</button>
      <button>720p</button><button>10 giây</button><button>x1</button>
      <flow-base-prompt-box><div contenteditable="true"></div>
      <button id="send">Bắt đầu tạo</button></flow-base-prompt-box>
      <button>Tải xuống</button><button>1080p Đã tăng độ phân giải</button>''')
    page.evaluate('''() => {
      const box=document.querySelector('flow-base-prompt-box');
      window.sent=[];
      box.addEventListener('paste',()=>{
        box.querySelector('[contenteditable]').remove();
        const editor=document.createElement('div');editor.contentEditable='true';box.prepend(editor);
        const image=new Image();image.alt='Ingredient image';
        image.src='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>';box.append(image);
      });
      document.querySelector('#send').onclick=()=>{
        sent.push(box.querySelector('[contenteditable]').innerText);
        const tile=document.createElement('flow-video-tile');
        tile.innerHTML='<img src="new.png" alt="Generated video thumbnail"><button>More options</button>';
        document.body.append(tile);
      };
      window.chrome={runtime:{sendMessage:async m=>{
        if(m.type==='assets')return {ok:true,assets:[{name:'image.png',mime:'image/png',base64:btoa('fixture')}]};
        if(m.type==='key'&&m.text!==undefined)document.activeElement.textContent=m.text;
        return {ok:true,path:'video.mp4'};
      }}};
    }''')
    scripts(page)
    task={'id':'test','payload':{'model':'Omni 1.1 Flash','prompt':'Animate the leaves gently','timeout':2}}
    if collect:
        page.evaluate("document.body.insertAdjacentHTML('beforeend','<flow-video-tile><img src=old.png>Animate the leaves gently<button>Tuỳ chọn khác</button></flow-video-tile>')")
    result=page.evaluate('args=>lofiProviderTest.providerMap[args.collect?"collect_video":"flow_video"](args.task)',{'task':task,'collect':collect})
    assert page.evaluate('sent')==([] if collect else [task['payload']['prompt']])
    assert not page.evaluate('Boolean(window.clickedSidebar)')
    assert result['path']=='video.mp4'


def test_image_paste_delivers_file_to_editor_without_os_clipboard(page):
    page.set_content('<div contenteditable="true" id="editor"></div>')
    page.add_script_tag(path=str(ROOT/'browser_extensions/lofi_automation/shared.js'))
    page.evaluate("""() => {
      document.querySelector('#editor').addEventListener('paste',event=>{
        window.received={name:event.clipboardData.files[0].name,
          type:event.clipboardData.files[0].type,size:event.clipboardData.files[0].size};
        event.preventDefault();
      });
      lofi.pasteImage(document.querySelector('#editor'),{
        name:'scene.png',mime:'image/png',base64:btoa('image fixture')
      });
    }""")
    assert page.evaluate('received')=={'name':'scene.png','type':'image/png','size':13}


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


@pytest.mark.parametrize("submit_mode", ["button", "enter", "existing_chat", "project_layout"])
def test_first_image_transferred_without_second_prompt(page, submit_mode):
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
        const user=document.createElement('div');user.dataset.messageAuthorRole='user';
        for(const word of composer.textContent.split(' ')){
          const paragraph=document.createElement('p');paragraph.textContent=word;user.append(paragraph);
        }document.querySelector('#turns').append(user);
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
          if(m.key==='Enter')window.submitPrompt();
        }
        return {ok:true,cancel_requested:false,path:'final.png'};
      }}};
    }''',images)
    if submit_mode=="project_layout":
        page.evaluate('''reference => {
          const send=document.querySelector('[data-testid="send-button"]');
          const submit=send.onclick;
          const originalBridge=chrome.runtime.sendMessage;
          let first=true;
          chrome.runtime.sendMessage=async m=>{
            if(m.type==='image-file'&&first){first=false;messages.push(m);return {ok:true,reference:true};}
            return originalBridge(m);
          };
          send.onclick=()=>{
            submit();
            document.querySelector('[data-message-author-role="user"]').removeAttribute('data-message-author-role');
            const game=document.createElement('canvas');
            game.width=470;game.height=485;game.dataset.testid='image-gen-loading-game-board';
            const attached=new Image();attached.src=reference;
            document.querySelector('[data-message-author-role="assistant"]').prepend(game,attached);
          };
        }''',images[1])
    if submit_mode == "enter":
        page.evaluate("""() => {
          const button=document.querySelector('[data-testid="send-button"]');
          window.submitPrompt=button.onclick;
          button.remove();
        }""")
    scripts(page)
    task={'id':'test','payload':{'prompt1':'Create the scene','timeout':30}}
    editing=submit_mode=="existing_chat"
    if editing:
        task['kind']='collect_image'
        task['payload']['url']=page.evaluate('location.origin+location.pathname')
        page.evaluate("""() => {
          document.querySelector('#prompt-textarea').textContent='Create the scene';
          document.querySelector('[data-testid="send-button"]').click();
        }""")
    result=page.evaluate('task=>lofiProviderTest.providerMap[task.kind||"generate_image"](task)',task)
    assert result['path']=='final.png'
    assert page.evaluate('sends')==['Create the scene']
    encoded=page.evaluate("messages.filter(m=>m.type==='image-file').at(-1).image")
    if submit_mode=='project_layout':
        assert page.evaluate("messages.filter(m=>m.type==='image-file').length")==2
    output=Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert output.size==(480,360) and output.getpixel((0,0))[:3]==(255,0,0)
    if editing:
        assert not page.evaluate("messages.some(m=>m.type==='assets')")
    else:
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
// Excel returns its own download ID even when Chrome emits no tab download event.
(async()=>{
  vm.runInContext("active={id:'job',tab:7,kind:'excel_snapshot',url:'https://cmcuni-my.sharepoint.com/:x:/r/personal/example/_layouts/15/Doc.aspx?sourcedoc=%7B84A9BC3B-18E1-4153-86CB-2F26564008D9%7D'}",context);
  let request;
  chrome.downloads.download=async options=>{request=options;return 81};
  chrome.downloads.search=async ({id})=>id===81?[{state:'complete',filename:'D:/download/LoFiAutomation/job/asset.xlsx'}]:[];
  vm.runInContext("api=async (path,body)=>path==='download'?{ok:true,path:body.source}:{cancel_requested:false}",context);
  const result=await new Promise(resolve=>handlers.message({type:'download-workbook',id:'job'},{tab:{id:7}},resolve));
  const excel=result.path?.endsWith('/job/asset.xlsx') && request.filename==='LoFiAutomation/job/asset.xlsx' &&
    request.url.startsWith('https://cmcuni-my.sharepoint.com/personal/example/_layouts/15/download.aspx?UniqueId=');
  console.log(JSON.stringify({before,after,excel}));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    run=subprocess.run([node,'-e',script,str(ROOT/'browser_extensions/lofi_automation/background.js')],
                       capture_output=True,text=True,check=True)
    assert json.loads(run.stdout)=={'before':True,'after':True,'excel':True}
