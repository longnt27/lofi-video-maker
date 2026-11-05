(() => {
  const L=globalThis.lofi;
  async function download(task,button){
    await L.bridge('arm-download',task.id);button.click();return L.bridge('finish-download',task.id);
  }
  const normalize=text=>text.normalize('NFKC').replace(/\s+/g,'');
  function userTurns(){return L.all('[data-message-author-role="user"],article[data-turn="user"]');}

  function resultImage(turn,skipped){
    const next=userTurns().find(el=>turn.compareDocumentPosition(el)&Node.DOCUMENT_POSITION_FOLLOWING);
    return L.all('main img,article img,[data-message-author-role="assistant"] img,main canvas').find(el=>{
      const width=el.naturalWidth||el.width, height=el.naturalHeight||el.height;
      const container=el.closest('article,[data-testid^="conversation-turn"]');
      return !skipped.has(el.currentSrc||el.src||el) && width>=256 && height>=144 && !(el instanceof HTMLImageElement && !el.complete) &&
        !el.closest('form,[data-message-author-role="user"],[data-testid*="loading"],[role="progressbar"]') && !turn.contains(el) &&
        (el.tagName!=='CANVAS'||el.closest('[data-message-author-role="assistant"]')) &&
        !container?.querySelector('[data-message-author-role="user"]') &&
        (turn.compareDocumentPosition(el)&Node.DOCUMENT_POSITION_FOLLOWING) &&
        (!next||(el.compareDocumentPosition(next)&Node.DOCUMENT_POSITION_FOLLOWING));
    });
  }

  async function transferImage(task,turn){
    const id=task.id;
    await L.progress(id,'ChatGPT: đang lọc ảnh tham chiếu để lấy ảnh mới');
    const skipped=new Set(), deadline=Date.now()+task.payload.timeout*1000;
    while(Date.now()<deadline){
      await L.check(id);
      const image=resultImage(turn,skipped);
      if(!image){await L.pause(1500);continue;}
      const result=await transferCandidate(task,image);
      if(!result.reference)return result;
      skipped.add(image.currentSrc||image.src||image);
    }
    throw new Error('ChatGPT: hết thời gian chờ ảnh mới của kịch bản.');
  }

  async function transferCandidate(task,image){
    const id=task.id;
    const canvas=document.createElement('canvas');
    canvas.width=image.naturalWidth||image.width;
    canvas.height=image.naturalHeight||image.height;
    let encoded;
    try{
      canvas.getContext('2d').drawImage(image,0,0);
      encoded=canvas.toDataURL('image/png').split(',')[1];
    }catch{
      const card=image.closest('article,[data-testid^="conversation-turn"],[data-message-author-role="assistant"]')||image.parentElement;
      image.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
      const save=await L.until(()=>L.find(/^(download|save)( image)?$|^tải (ảnh|xuống)( xuống| ảnh)?$/i,card));
      return download(task,save);
    }
    return L.bridge('image-file',id,{image:encoded});
  }

  async function chatImage(task){
    const {id,payload:p}=task;
    const matches=turn=>normalize(turn.textContent).includes(normalize(p.prompt1));
    const promptBlock=()=>L.all('article,[data-turn="user"],main div,main p')
      .filter(el=>!el.closest('form,[contenteditable="true"]')&&matches(el)).at(-1);
    if(task.kind==='collect_image'){
      const conversation=location.pathname.match(/\/c\/([0-9a-f-]{36})\/?$/i);
      if(location.origin+location.pathname!==p.url&&
        !(location.origin==='https://chatgpt.com'&&conversation&&p.url===location.origin+'/c/'+conversation[1]))
        throw new Error('Sai hội thoại ảnh.');
      await L.bridge('diagnostic',id,{text:JSON.stringify({
        path:location.pathname,visibility:document.visibilityState,
        users:userTurns().length,articles:L.all('article').length,
        images:L.all('img').map(el=>({width:el.naturalWidth,height:el.naturalHeight,alt:el.alt})),
        text:document.body.innerText.slice(-9000)
      },null,2)});
      await L.progress(id,'ChatGPT: đang tìm tin nhắn gốc trong hội thoại');
      let first;
      try{first=await L.until(()=>userTurns()[0]||promptBlock(),60000);}
      catch{throw new Error('ChatGPT: trang đã mở nhưng chưa tìm thấy tin nhắn người dùng.');}
      if(!matches(first))L.find(/^(xem thêm|show more|read more)$/i,first)?.click();
      try{await L.until(()=>matches(first));}
      catch{throw new Error('ChatGPT: đã tìm thấy tin nhắn nhưng nội dung chưa khớp kịch bản Excel.');}
      return transferImage(task,first);
    }
    await L.progress(id,'ChatGPT: chờ project và khung nhập tải xong');
    let composer;
    try{
      composer=await L.until(()=>L.all('#prompt-textarea,[data-testid="composer-input"],div[contenteditable="true"][role="textbox"]').find(L.visible),120000);
    }catch{throw new Error('ChatGPT: project chưa tải xong khung nhập sau 120 giây. Kiểm tra đăng nhập và quyền truy cập project.');}
    const form=composer.closest('form,[data-type="unified-composer"]');
    if(!form)throw new Error('Chưa tìm thấy khung nhập prompt ChatGPT.');
    await L.progress(id,'ChatGPT: đính kèm ảnh tham chiếu');
    const file=await L.until(()=>L.all('input[type="file"]',form).find(input=>input.multiple)||
      L.all('input[type="file"]').find(input=>input.multiple&&(!input.accept||input.accept.includes('image'))));
    await L.upload(file,(await L.bridge('assets',id)).assets);
    await L.pause(2500);
    const previous=new Set([...userTurns(),promptBlock()].filter(Boolean));
    await L.type(id,composer,p.prompt1);
    const send=L.all('button',form).find(el=>L.visible(el)&&
      (el.id==='composer-submit-button'||el.dataset.testid==='send-button'||/^(send|gửi)(\s|$)/i.test(L.name(el))));
    if(send)await L.until(()=>!send.disabled&&send.getAttribute('aria-disabled')!=='true',60000);
    await L.check(id);await L.progress(id,'ChatGPT: gửi prompt tạo ảnh',true);
    if(send)send.click();else await L.key(id,'Enter');
    const turn=await L.until(()=>{
      if(!/\/c\/[0-9a-f-]{36}\/?$/i.test(location.pathname)&&location.hostname==='chatgpt.com')return null;
      const message=userTurns().find(el=>!previous.has(el)&&matches(el))||promptBlock();
      return message&&!previous.has(message)?message:null;
    },90000);
    return transferImage(task,turn);
  }
  async function flowVideo(task){
    const p=task.payload,id=task.id;
    const composer=await L.until(()=>L.all('flow-base-prompt-box').find(L.visible));
    const onboarding=L.find(/^got it.*onboarding/i);if(onboarding)onboarding.click();
    await L.progress(id,'Flow: mở cài đặt video');
    const agent=L.all('button.agent-mode-chip').find(L.visible);if(agent?.getAttribute('aria-pressed')==='true')agent.click();
    (await L.until(()=>L.find(/^(settings trigger|điều kiện kích hoạt cài đặt)$/i))).click();
    for(const [label,pattern] of [
      ['Video',/^(videocam\s*)?video$/i],
      ['Thành phần',/^(chrome_extension\s*)?(ingredients|thành phần)$/i],
      ['16:9',/^(crop_16_9\s*)?16:9$/i]
    ]){
      await L.progress(id,'Flow: đang chọn '+label);
      try{
        const choice=await L.until(()=>L.all('[role="radio"],[role="menuitemradio"]').find(el=>
          L.visible(el)&&[L.name(el),el.innerText].some(value=>pattern.test(value.trim()))));
        if(choice.getAttribute('aria-checked')!=='true')choice.click();
      }
      catch{throw new Error('Flow: không tìm thấy lựa chọn '+label+' trong menu.');}
    }
    await L.progress(id,'Flow: chọn model '+p.model);
    const modelPattern=new RegExp('^'+p.model.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?:\\s|$)','i');
    const model=await L.until(()=>L.find(/^(chọn nhóm mô hình|select model family|select model)$/i)||L.find(modelPattern));
    if(!modelPattern.test(model.innerText.trim())){
      model.click();
      (await L.until(()=>L.find(modelPattern))).click();
      await L.until(()=>modelPattern.test((L.find(/^(chọn nhóm mô hình|select model family|select model)$/i)||model).innerText.trim()));
    }
    (await L.until(()=>L.find(/^720p$/i))).click();
    await L.progress(id,'Flow: chọn thời lượng 10 giây');
    try{(await L.until(()=>L.find(/^(10s|10 s|10 giây)$/i))).click();}
    catch{throw new Error('Flow: chưa tìm thấy tùy chọn 10 giây. Cần kiểm tra các thời lượng model hỗ trợ.');}
    (await L.until(()=>L.find(/^x1$/i))).click();
    await L.key(id,'Escape');
    const old=new Set(L.all('flow-video-tile img').map(img=>img.src));
    const field=await L.until(()=>L.all('[contenteditable="true"]',composer).find(L.visible));
    const {assets}=await L.bridge('assets',id);
    if(assets.length!==1)throw new Error('Cần đúng một ảnh đầu vào cho video.');
    await L.progress(id,'Flow: đang dán ảnh và chờ ảnh tải xong');
    L.pasteImage(field,assets[0]);
    let added=false;
    try{
      await L.until(()=>{
        if(L.all('img[alt="Ingredient image"],img[alt="Hình ảnh thành phần"],img[alt="Ảnh thành phần"]',composer).some(img=>img.complete&&img.naturalWidth>0))return true;
        const add=L.find(/^add to prompt$|^thêm vào (câu lệnh|lời nhắc)$/i);
        if(!added&&add&&!add.disabled&&add.getAttribute('aria-disabled')!=='true'){add.click();added=true;}
        return false;
      },90000);
    }catch{throw new Error('Flow chưa xác nhận ảnh dán vào khung nhập. Đã dừng trước khi tạo video.');}
    // Pasting an ingredient can replace the editor, invalidating the old node.
    const currentEditor=()=>L.all('flow-base-prompt-box').filter(L.visible)
      .flatMap(box=>L.all('[contenteditable="true"]',box)).find(el=>L.visible(el)&&el.isConnected);
    const promptEditor=await L.until(currentEditor);
    await L.progress(id,'Flow: nhập prompt animation');
    await L.type(id,promptEditor,p.prompt);
    await L.until(()=>normalize(currentEditor()?.innerText||'').includes(normalize(p.prompt)));
    const activeComposer=promptEditor.closest('flow-base-prompt-box')||composer;
    const send=await L.until(()=>L.find(/^(start generation|bắt đầu tạo)$/i,activeComposer));
    await L.check(id);await L.progress(id,'Flow: '+p.model+' · Thành phần · 16:9 · 720p · 10 giây · x1',true);send.click();
    const deadline=Date.now()+p.timeout*1000;let tile;
    while(Date.now()<deadline){
      await L.check(id);
      tile=L.all('flow-video-tile').find(el=>
        L.all('img,video',el).some(media=>!old.has(media.src||media.currentSrc)) &&
        L.find(/^(more options|lựa chọn khác|các lựa chọn khác|tu[ỳy] chọn khác)|^more_vert$/i,el)
      );
      if(tile)break;
      await L.pause(3000);
    }
    if(!tile)throw new Error('Flow did not show a new completed clip before timeout');
    return downloadFlowVideo(task,tile);
  }
  async function downloadFlowVideo(task,tile){
    const id=task.id;
    await L.progress(id,'Flow: tải video đã tạo');
    await L.hover(id,tile);
    const more=await L.until(()=>L.find(/^(more options|lựa chọn khác|các lựa chọn khác|tu[ỳy] chọn khác)|^more_vert$/i,tile),60000);
    await L.bridge('arm-download',id);
    more.click();
    await L.pause(500);
    const overlay = () => document.querySelector('.cdk-overlay-container') || document.body;
    const dl=await L.until(()=>L.find(/^(download|tải xuống)$/i, overlay()),20000);
    await L.hover(id,dl);
    dl.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,pointerType:'mouse'}));
    dl.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
    dl.dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));
    await L.pause(600);
    const option=()=>L.find(/^(1080p|original size|kích thước gốc|720p)\b/i, overlay()) ||
                     L.find(/\b(1080p|original size|kích thước gốc|720p)\b/i, overlay());
    let target=option();
    if(!target){
      dl.click();
      await L.pause(800);
      target=option();
    }
    if(!target){
      const items=L.all('[role="menuitem"],button', overlay()).map(el=>el.innerText||L.name(el)).filter(Boolean);
      throw new Error('Flow: không tìm thấy nút chất lượng trong overlay. Các nút thấy: ' + (items.join(' // ') || 'trống'));
    }
    target.click();
    return L.bridge('finish-download',id);
  }
  async function collectVideo(task){
    await L.progress(task.id,'Flow: tìm video đã tạo theo prompt');
    const tile=await L.until(()=>{
      const videos=L.all('flow-video-tile').filter(el=>
        L.all('img,video',el).length &&
        L.find(/^(more options|lựa chọn khác|các lựa chọn khác|tu[ỳy] chọn khác)|^more_vert$/i,el)
      );
      const normPrompt=normalize(task.payload.prompt||'');
      const matches=videos.filter(el=>
        [el.innerText,...L.all('[title],[aria-label]',el).map(L.name)].some(label=>{
          const normLabel=normalize(label);
          return normLabel.includes(normPrompt.slice(0,24)) ||
                 normLabel.includes(normPrompt.slice(0,16)) ||
                 (normLabel.length>=10 && normPrompt.includes(normLabel.slice(0,16)));
        }));
      if(matches.length>0)return matches[matches.length-1];
      return videos.length ? videos[videos.length-1] : null;
    },60000);
    return downloadFlowVideo(task,tile);
  }
  const nameBox=()=>L.all('input[aria-label*="Name Box"],input[aria-label*="Name box"],input[aria-label*="Hộp Tên"],input[aria-label*="Hộp tên"],input[name="NameBox"]').find(L.visible);
  function formula(cellCoord){
    const checkbox = document.querySelector('[role="checkbox"], [aria-checked]');
    if(checkbox && checkbox.getAttribute('aria-checked') !== null){
      return checkbox.getAttribute('aria-checked').toUpperCase();
    }
    const candidates = L.all('[role="textbox"][aria-label*="formula" i], input[aria-label*="formula" i], textarea[aria-label*="formula" i], div[contenteditable="true"][id*="formula" i], [aria-label="formula bar" i], [aria-label="Formula Bar" i], [aria-label="Thanh công thức" i]')
      .filter(e => L.visible(e) && !['a','button'].includes(e.tagName.toLowerCase()) && !/expand|collapse/i.test(e.getAttribute('aria-label')||''));
    for(const el of candidates){
      const t=(el.value!==undefined?el.value:(el.innerText||el.textContent||'')).trim();
      if(t && t.toLowerCase()!=='formula bar') return t.replace(/^=/,'').trim();
    }
    const lines = (document.body.innerText||'').split('\n').map(l=>l.trim()).filter(Boolean);
    const shareIdx = lines.lastIndexOf('Share');
    if(shareIdx!==-1 && lines[shareIdx+1] && !/selected by|kich_ban|prompts|youtube|huong_dan|editing view/i.test(lines[shareIdx+1])){
      return lines[shareIdx+1].replace(/^=/,'').trim();
    }
    const cells=L.all('[role="button"],[role="gridcell"]').filter(L.visible);
    const targetCell=cells.find(c=>{
      const a=c.getAttribute('aria-label')||c.innerText||'';
      return cellCoord ? a.includes(cellCoord) : /inside frozen region/i.test(a);
    }) || cells.find(c=>(c.getAttribute('aria-label')||'').includes('Inside frozen region'));
    if(targetCell){
      const a=(targetCell.getAttribute('aria-label')||targetCell.innerText||'').trim();
      const first=a.split(/\s+/)[0];
      if(first) return first.replace(/^=/,'').trim();
    }
    return null;
  }
  async function gotoCell(id,cell){
    const box=await L.until(nameBox);
    box.focus();
    await L.key(id,'ControlA');
    await L.text(id,cell);
    await L.key(id,'Enter');
    await L.pause(800);
    box.blur();
    await L.pause(200);
    if(box.value.replaceAll('$','').toUpperCase()!==cell.toUpperCase())throw new Error('Excel cell selection was not confirmed');
  }
  async function excelSnapshot(task){
    const id=task.id;
    const workbook=new URL(task.payload.url);
    const documentId=workbook.searchParams.get('sourcedoc')?.replace(/^\{|\}$/g,'');
    if(workbook.hostname==='cmcuni-my.sharepoint.com' &&
       /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(documentId) &&
       /\/_layouts\/15\/Doc\.aspx$/i.test(workbook.pathname)){
      await L.progress(id,'Excel: tải và kiểm tra workbook của lượt này');
      return L.bridge('download-workbook',id);
    }
    await L.until(()=>nameBox()||L.find(/^(file|tệp)$/i),60000);
    (await L.until(()=>L.find(/^(file|tệp)$/i))).click();
    const save=await L.until(()=>L.find(/^(save as|export|xuất|lưu như|lưu dưới dạng)$/i));save.click();
    const copy=await L.until(()=>L.find(/^(download a copy|download a copy.*|tải xuống bản sao|tải xuống một bản sao)$/i));
    await L.progress(id,'Excel: tải snapshot workbook hiện tại');return download(task,copy);
  }
  async function excelTick(task){
    const p=task.payload,id=task.id;
    const sheet=await L.until(()=>L.find(new RegExp('^'+p.sheet+'$','i')),60000);sheet.click();
    await gotoCell(id,p.id_cell);
    const actual=await L.until(()=>formula(p.id_cell)===p.id?formula(p.id_cell):null,8000).catch(()=>formula(p.id_cell));
    if(actual!==p.id)throw new Error('Excel row ID does not match: expected ' + p.id + ', got ' + JSON.stringify(actual));
    await gotoCell(id,p.cell);
    const before=await L.until(()=>formula(p.cell),5000).catch(()=>formula(p.cell));
    if(!/^(TRUE|FALSE|ĐÚNG|SAI|0|1)$/i.test(before||''))throw new Error('Cannot read checkbox boolean: ' + JSON.stringify(before) + '; refusing to toggle blindly');
    if(!/^(TRUE|ĐÚNG|1)$/i.test(before)){
      // Trusted keystrokes are dispatched only to this bot-owned tab through CDP.
      await L.key(id,'Space');
      await gotoCell(id,p.cell);
      const after=await L.until(()=>/^(TRUE|ĐÚNG|1)$/i.test(formula(p.cell)||'')?formula(p.cell):null,8000).catch(()=>formula(p.cell));
      if(!/^(TRUE|ĐÚNG|1)$/i.test(after||''))throw new Error('Excel checkbox change was not confirmed: ' + JSON.stringify(after));
    }
    // Wait for an actual saved state instead of assuming a successful click means cloud persistence.
    await L.until(()=>L.all('[aria-label],[title],[role="status"]').some(el=>L.visible(el)&&/^(saved|all changes saved|đã lưu|đã lưu tất cả thay đổi)(\b|$)/i.test(L.name(el))),30000);
    return {checked:true};
  }
  const host=location.hostname;
  const providerMap={generate_image:chatImage,collect_image:chatImage,flow_video:flowVideo,collect_video:collectVideo,excel_snapshot:excelSnapshot,excel_tick:excelTick,
    flow_music:async task=>{const result=await globalThis.lofiProviders.flow_music(task);if(!result?.ok)throw new Error(result?.error||'Music generation failed');return {};}};
  function capabilities(){
    if(host==='chatgpt.com')return ['generate_image','collect_image'];
    if(host==='flow.google.com')return ['flow_video','collect_video'];
    if(host==='flowmusic.app'||host==='www.flowmusic.app')return ['flow_music','collect_music'];
    if((host==='cmcuni-my.sharepoint.com'||host.endsWith('.officeapps.live.com'))&&(nameBox()||L.find(/^(file|tệp)$/i)))return ['excel_snapshot','excel_tick'];
    return [];
  }
  providerMap.collect_music=async task=>{
    if(!task.payload.song_url)throw new Error('Thiếu link bài nhạc cần tải.');
    return providerMap.flow_music(task);
  };
  if(globalThis.__LOFI_PROVIDER_TEST__){globalThis.lofiProviderTest={providerMap,formula,gotoCell};return;}
  let busy=false;
  chrome.runtime.onMessage.addListener((m,_sender,reply)=>{
    if(m.type!=='run')return;
    if(busy||!capabilities().includes(m.task.kind)){reply({ok:false,error:'Wrong or busy frame'});return;}
    busy=true;reply({ok:true});
    (async()=>{
      try{const result=await providerMap[m.task.kind](m.task);await L.bridge('result',m.task.id,{ok:true,result});}
      catch(error){
        const structure={
          path:location.pathname,
          users:userTurns().map(el=>({tag:el.tagName,chars:el.textContent.length,
            matches:normalize(el.textContent).includes(normalize(m.task.payload.prompt1||''))})),
          images:L.all('main img,article img,main canvas').map(el=>({
            tag:el.tagName,width:el.naturalWidth||el.width,height:el.naturalHeight||el.height,
            loaded:el.complete,user:Boolean(el.closest('[data-message-author-role="user"]')),
            form:Boolean(el.closest('form')),testId:el.closest('[data-testid]')?.getAttribute('data-testid')
          })),
          controls:L.all('[role],button,label').filter(el=>L.visible(el)).slice(-100).map(el=>({tag:el.tagName,role:el.getAttribute('role'),name:L.name(el).slice(0,180)})),
          buttons:L.all('main button').map(L.name).filter(Boolean).slice(-25)
        };
        try{await L.bridge('diagnostic',m.task.id,{text:JSON.stringify(structure,null,2)+'\n'+document.body.innerText.slice(-12000)});}catch{}
        try{await L.bridge('result',m.task.id,{ok:false,error:String(error)});}catch{}
      }
      finally{busy=false;}
    })();
  });
  const ready=()=>chrome.runtime.sendMessage({type:'ready',kinds:capabilities()}).catch(()=>{});
  ready();setInterval(ready,3000);
})();
