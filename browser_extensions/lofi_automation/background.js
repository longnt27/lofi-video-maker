importScripts('config.js');
let polling=false, releasing=false, active=null, watch=null;
const frames=new Map();
const pause=ms=>new Promise(r=>setTimeout(r,ms));
let identityPromise;
function identity(){
  if(!identityPromise)identityPromise=chrome.storage.local.get('installationId').then(async saved=>{
    const id=saved.installationId||crypto.randomUUID();
    if(!saved.installationId)await chrome.storage.local.set({installationId:id});return id;
  });return identityPromise;
}
async function api(path,body){
  const instance=await identity();
  const response=await fetch(LOFI_CONFIG.url+'/bridge/'+path,{method:body===undefined?'GET':'POST',
    headers:{'Authorization':'Bearer '+LOFI_CONFIG.token,'X-LoFi-Instance':instance,'X-LoFi-Single-Image':'3','Content-Type':'application/json'},
    ...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(25000)});
  if(!response.ok)throw new Error(await response.text());return response.json();
}
const sendKey=async(key,text)=>{
  const target={tabId:active.tab};
  if(text!==undefined){await chrome.debugger.sendCommand(target,'Input.insertText',{text});return;}
  const spec={Enter:[13,'Enter'],Escape:[27,'Escape'],Space:[32,'Space'],Tab:[9,'Tab'],F2:[113,'F2'],ControlA:[65,'KeyA']};
  if(!spec[key])throw new Error('Unsupported key');
  const [code,physical]=spec[key],modifiers=key==='ControlA'?2:0;
  const params={key:key==='ControlA'?'a':key==='Space'?' ':key,code:physical,windowsVirtualKeyCode:code,nativeVirtualKeyCode:code,modifiers};
  await chrome.debugger.sendCommand(target,'Input.dispatchKeyEvent',{type:'keyDown',...params});
  await chrome.debugger.sendCommand(target,'Input.dispatchKeyEvent',{type:'keyUp',...params});
};
// A referrer may be origin-only. Never use it to capture another tab's download.
function match(item){return !!watch&&[item.url,item.finalUrl].some(url=>url&&watch.verifiedUrls.has(url));}
function potential(item){if(!watch)return false;if(match(item))return true;
  try{return new URL(item.referrer).origin===new URL(watch.url).origin||item.url?.startsWith('blob:'+new URL(watch.url).origin+'/');}catch{return false;}}
chrome.downloads.onDeterminingFilename.addListener((item,suggest)=>{
  if(watch?.direct && item.byExtensionId===chrome.runtime.id && item.url===watch.url){
    suggest({filename:`LoFiAutomation/${watch.id}/asset.xlsx`,conflictAction:'uniquify'});return;
  }
  const ext=(item.filename.match(/\.(xlsx|png|jpe?g|webp|mp4|mp3|wav|m4a|flac)$/i)||[])[1];
  if(!ext||!potential(item)){suggest();return;}
  const expected=watch;
  (async()=>{const deadline=Date.now()+4000;while(watch===expected&&Date.now()<deadline&&!match(item))await pause(50);
    if(watch===expected&&match(item))suggest({filename:`LoFiAutomation/${watch.id}/asset.${ext.toLowerCase()}`,conflictAction:'uniquify'});else suggest();
  })();return true;
});
chrome.downloads.onCreated.addListener(item=>{
  if(watch){watch.candidates.push(item);if(match(item)&&watch.downloadId===undefined)watch.downloadId=item.id;}
});
chrome.debugger.onEvent.addListener((source,method,params)=>{
  if(!watch||source.tabId!==active?.tab||!['Page.downloadWillBegin','Browser.downloadWillBegin'].includes(method))return;
  watch.verifiedUrls.add(params.url);
  const item=watch.candidates.find(match);if(item&&watch.downloadId===undefined)watch.downloadId=item.id;
});
async function downloaded(id){
  if(!watch||watch.id!==id)throw new Error('Download not armed');
  const end=Date.now()+(active?.kind==='collect_video'||active?.kind==='flow_video'?600000:120000);
  while(Date.now()<end){
    if((await api('state',{id})).cancel_requested)throw new Error('Stopped');
    if(watch.downloadId!==undefined){const [d]=await chrome.downloads.search({id:watch.downloadId});
      if(d?.state==='interrupted')throw new Error('Chrome download interrupted: '+d.error);
      if(d?.state==='complete')return api('download',{id,source:d.filename});}
    await pause(1000);
  }throw new Error('Download timed out');
}
async function release(close=false){
  releasing=true;const previous=active;active=null;watch=null;
  try{await chrome.storage.session.remove('active');if(previous){try{await chrome.debugger.detach({tabId:previous.tab});}catch{}if(close&&!previous.reused){try{await chrome.tabs.remove(previous.tab);}catch{}}}}
  finally{releasing=false;}
}
async function findPendingMusicTab(task){
  const tabs=await chrome.tabs.query({url:['https://www.flowmusic.app/*','https://flowmusic.app/*']});
  const probe={type:'probe-music-preference',payload:task.payload};
  const matches=[];
  for(const tab of tabs){
    let response=await chrome.tabs.sendMessage(tab.id,probe,{frameId:0}).catch(()=>null);
    if(!response){
      // An unpacked extension reload invalidates the scripts in already-open tabs.
      await chrome.scripting.executeScript({target:{tabId:tab.id},files:['shared.js','music.js','providers.js']}).catch(()=>{});
      response=await chrome.tabs.sendMessage(tab.id,probe,{frameId:0}).catch(()=>null);
    }
    if(response?.matches)matches.push(tab);
  }
  if(matches.length!==1)throw new Error('Cần đúng một tab Flow Music đang hiện lựa chọn của lượt này. Giữ tab đó mở rồi bấm Tiếp tục.');
  return matches[0];
}
async function poll(){
  if(polling||active||releasing)return {ok:true};polling=true;let claimed=false,task;
  try{
    const next=await api('next');
    if(next.revision && next.revision!==LOFI_CONFIG.revision){
      const saved=await chrome.storage.local.get('reloadRevision');
      if(saved.reloadRevision!==next.revision){
        await chrome.storage.local.set({reloadRevision:next.revision});
        chrome.runtime.reload();
      }
      return {ok:false,error:'Reload the Studio-generated extension folder.'};
    }
    task=next.task;if(!task)return {ok:true};
    await api('claim',{id:task.id});claimed=true;
    // A background tab can defer ChatGPT's conversation rendering indefinitely.
    // Give the job an active tab in its own unfocused window.
    const reused=Boolean(task.payload.recover_preference);
    let tab;
    if(reused)tab=await findPendingMusicTab(task);
    else{
      const window=await chrome.windows.create({url:task.payload.url,type:'normal',focused:false});
      tab=window.tabs?.[0]||(await chrome.tabs.query({windowId:window.id}))[0];
    }
    if(!tab)throw new Error('Chrome did not create the automation tab.');
    active={id:task.id,tab:tab.id,kind:task.kind,url:task.payload.url,reused};await chrome.storage.session.set({active});
    await chrome.debugger.attach({tabId:tab.id},'1.3');
    await chrome.debugger.sendCommand({tabId:tab.id},'Page.enable');
    try{await chrome.debugger.sendCommand({tabId:tab.id},'Page.setDownloadBehavior',{behavior:'allow'});}catch{}
    const deadline=Date.now()+90000;let frame;
    while(Date.now()<deadline){
      const choices=frames.get(tab.id)||[];frame=choices.find(f=>f.kinds.includes(task.kind));if(frame)break;
      if((await api('state',{id:task.id})).cancel_requested)throw new Error('Stopped');await pause(700);
    }
    if(!frame)throw new Error('No compatible page detected. Check this profile login or the page interface.');
    const result=await chrome.tabs.sendMessage(tab.id,{type:'run',task},{frameId:frame.id});
    if(!result?.ok)throw new Error(result?.error||'Could not start task');return {ok:true};
  }catch(error){if(claimed){try{await api('result',{id:task.id,ok:false,error:String(error)});}catch{}await release();}return {ok:false,error:String(error)};}
  finally{polling=false;}
}
chrome.runtime.onMessage.addListener((m,sender,reply)=>{
  const handle=async()=>{
    if(m.type==='profile-info')return api('profile');
    if(m.type==='activate-profile'){const result=await api('activate',{});void poll();return result;}
    if(m.type==='rename-profile')return api('rename',{label:m.label});
    if(m.type==='ready'){
      if(!sender.tab)return {ok:false};const list=frames.get(sender.tab.id)||[];
      frames.set(sender.tab.id,[...list.filter(f=>f.id!==sender.frameId),{id:sender.frameId,kinds:m.kinds}]);
      void poll();return {ok:true};}
    if(m.type==='tick')return poll();
    if(!active){const saved=await chrome.storage.session.get('active');active=saved.active||null;}
    if(!active||m.id!==active.id||sender.tab?.id!==active.tab)throw new Error('Not the active task tab');
    if(m.type==='state')return api('state',{id:m.id});
    if(m.type==='progress')return api('progress',{id:m.id,message:m.message||m.phase,submitted:m.submitted||['submitting','submitted','downloading'].includes(m.phase),session_url:active.kind.startsWith('excel')?active.url:(m.session_url||sender.url),song_url:m.song_url});
    if(m.type==='image-file')return api('image-file',{id:m.id,image:m.image});
    if(m.type==='assets')return api('assets/'+m.id);
    if(m.type==='download-workbook'){
      const workbook=new URL(active.url);
      const url=new URL('download.aspx',workbook);
      const documentId=workbook.searchParams.get('sourcedoc')?.replace(/^\{|\}$/g,'');
      if(active.kind!=='excel_snapshot'||workbook.origin!=='https://cmcuni-my.sharepoint.com'||
         !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(documentId)){
        throw new Error('Invalid SharePoint workbook download');
      }
      url.pathname=url.pathname.replace(/^\/:x:\/r\//,'/');
      url.searchParams.set('UniqueId',documentId);
      watch={id:m.id,url:url.href,direct:true,verifiedUrls:new Set(),candidates:[]};
      watch.downloadId=await chrome.downloads.download({url:url.href,filename:`LoFiAutomation/${m.id}/asset.xlsx`,saveAs:false});
      return downloaded(m.id);
    }
    if(m.type==='key'){await sendKey(m.key,m.text);return {ok:true};}
    if(m.type==='hover'){
      if(!Number.isFinite(m.x)||!Number.isFinite(m.y))throw new Error('Invalid pointer target');
      await chrome.debugger.sendCommand({tabId:active.tab},'Input.dispatchMouseEvent',{type:'mouseMoved',x:m.x,y:m.y});
      return {ok:true};
    }
    if(m.type==='arm-download'){watch={id:m.id,url:sender.url,verifiedUrls:new Set(),candidates:[]};return {ok:true};}
    if(m.type==='finish-download')return downloaded(m.id);
    if(m.type==='diagnostic')return api('diagnostic',{id:m.id,text:m.text});
    if(m.type==='result'){
      try{return await api('result',{id:m.id,ok:m.ok,error:m.error,result:m.result});}
      finally{await release(!!m.ok);}}
    throw new Error('Unknown bridge message');
  };handle().then(reply).catch(error=>reply({ok:false,error:String(error)}));return true;
});
chrome.tabs.onRemoved.addListener(async id=>{frames.delete(id);if(active?.tab===id){try{await api('result',{id:active.id,ok:false,error:'Task tab was closed. No automatic resubmission.'});}catch{}await release();}});
chrome.debugger.onDetach.addListener(async(source,reason)=>{if(active?.tab===source.tabId&&reason!=='target_closed'){try{await api('result',{id:active.id,ok:false,error:'Browser debugger detached. No automatic resubmission.'});}catch{}active=null;watch=null;await chrome.storage.session.remove('active');}});
chrome.alarms.onAlarm.addListener(()=>{void poll();});
chrome.runtime.onInstalled.addListener(()=>{chrome.alarms.create('lofi-poll',{periodInMinutes:.5});void poll();});
chrome.runtime.onStartup.addListener(async()=>{const saved=await chrome.storage.session.get('active');if(saved.active){try{await api('result',{id:saved.active.id,ok:false,error:'Chrome restarted; check provider session before retrying.'});}catch{}await chrome.storage.session.remove('active');}chrome.alarms.create('lofi-poll',{periodInMinutes:.5});void poll();});
