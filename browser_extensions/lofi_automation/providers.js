(() => {
  const L=globalThis.lofi;
  async function download(task,button){
    await L.bridge('arm-download',task.id);button.click();return L.bridge('finish-download',task.id);
  }
  function generatedImages(){
    return L.all('[data-message-author-role="assistant"] img, img[alt*="Generated image"], img[alt*="Image created"]').filter(img=>img.complete&&img.naturalWidth>400&&img.naturalHeight>300);
  }
  async function imagePair(task){
    const p=task.payload,id=task.id;
    const composer=await L.until(()=>L.all('#prompt-textarea,[data-testid="composer-input"],div[contenteditable="true"][role="textbox"]').find(L.visible));
    const {assets}=await L.bridge('assets',id);
    const form=composer.closest('form')||composer.closest('[data-type="unified-composer"]');
    if(!form)throw new Error('Cannot identify the chat composer. Refusing to upload into project sources.');
    const file=await L.until(()=>L.all('input[type="file"]',form).find(input=>input.multiple));
    await L.upload(file,assets);
    // Wait for uploaded attachments to settle before the first paid generation.
    await L.pause(2500);
    for(const [index,prompt] of [p.prompt1,p.prompt2].entries()){
      const baseline=new Set(generatedImages().map(img=>img.currentSrc||img.src));
      await L.type(id,composer,prompt);
      const send=await L.until(()=>L.all('button[data-testid="send-button"],button[aria-label="Send prompt"],button[aria-label="Send message"],button[aria-label="Send"]').find(el=>L.visible(el)&&!el.disabled));
      await L.check(id);await L.progress(id,`ChatGPT: gửi prompt ảnh ${index+1}/2`,true);send.click();
      const deadline=Date.now()+p.timeout*1000;let image,stable=0;
      let rounds=0;
      while(Date.now()<deadline){
        await L.check(id);
        if(++rounds%5===0)await L.progress(id,`ChatGPT: đang tạo ảnh ${index+1}/2`,true);
        image=generatedImages().filter(img=>!baseline.has(img.currentSrc||img.src)).at(-1);
        const busy=L.all('button[data-testid="stop-button"],button[aria-label*="Stop generating"],button[aria-label*="Stop streaming"]').some(L.visible);
        stable=image&&!busy?stable+1:0;if(stable>=3)break;await L.pause(2000);
      }
      if(stable<3)throw new Error(`Image pass ${index+1} not confirmed. Check the saved conversation; no automatic resend.`);
      if(index===1){
        let card=image.closest('[data-message-author-role="assistant"]')||image.parentElement;
        image.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
        const save=await L.until(()=>L.find(/^(download|save)( image)?$/i,card));
        return download(task,save);
      }
    }
  }
  async function flowVideo(task){
    const p=task.payload,id=task.id;
    const composer=await L.until(()=>L.all('flow-base-prompt-box').find(L.visible));
    const onboarding=L.find(/^got it.*onboarding/i);if(onboarding)onboarding.click();
    const agent=L.all('button.agent-mode-chip').find(L.visible);if(agent?.getAttribute('aria-pressed')==='true')agent.click();
    (await L.until(()=>L.find(/^settings trigger$/i))).click();
    for(const pattern of [/^video$/i,/^frames$/i]){(await L.until(()=>L.find(pattern))).click();}
    const model=await L.until(()=>L.find(new RegExp(p.model.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i')));
    model.click();
    (await L.until(()=>L.find(/^10s$/i))).click();
    (await L.until(()=>L.find(/^x1$/i))).click();
    await L.key(id,'Escape');
    const old=new Set(L.all('flow-video-tile img[alt="Generated video thumbnail"]').map(img=>img.src));
    (await L.until(()=>L.find(/^start$/i,composer))).click();
    const upload=await L.until(()=>L.find(/^upload( image| file)?$/i));upload.click();
    const input=await L.until(()=>L.all('input[type="file"]').find(el=>!el.disabled));
    await L.upload(input,(await L.bridge('assets',id)).assets);
    (await L.until(()=>L.find(/^add to prompt$/i),60000)).click();
    await L.until(()=>L.all('img[alt="Ingredient image"]',composer).length);
    const field=await L.until(()=>L.all('[contenteditable="true"]',composer).find(L.visible));await L.type(id,field,p.prompt);
    const send=await L.until(()=>L.find(/^start generation$/i,composer));
    await L.check(id);await L.progress(id,'Flow: Omni Flash · x1 · 10s',true);send.click();
    const deadline=Date.now()+p.timeout*1000;let tile;
    while(Date.now()<deadline){await L.check(id);tile=L.all('flow-video-tile').find(el=>L.all('img[alt="Generated video thumbnail"]',el).some(img=>!old.has(img.src)));
      if(tile)break;await L.pause(3000);}
    if(!tile)throw new Error('Flow did not show a new completed clip before timeout');
    const more=await L.until(()=>L.find(/^more options$/i,tile));
    await L.bridge('arm-download',id);more.click();
    const dl=await L.until(()=>L.find(/^download$/i));
    dl.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,pointerType:'mouse'}));dl.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
    const original=await L.until(()=>L.find(/^original size$/i));original.click();return L.bridge('finish-download',id);
  }
  const nameBox=()=>L.all('input[aria-label*="Name Box"],input[aria-label*="Name box"],input[aria-label*="Hộp Tên"],input[aria-label*="Hộp tên"],input[name="NameBox"]').find(L.visible);
  function formula(){
    const el=L.all('[aria-label*="Formula Bar"],[aria-label*="Formula bar"],[aria-label*="Thanh công thức"],textarea[id*="formula"],div[contenteditable="true"][id*="formula"]').find(L.visible);
    return el?(el.value===undefined?el.innerText:el.value).trim():null;
  }
  async function gotoCell(id,cell){const box=await L.until(nameBox);box.focus();await L.key(id,'ControlA');await L.text(id,cell);await L.key(id,'Enter');await L.pause(700);
    if(box.value.replaceAll('$','').toUpperCase()!==cell.toUpperCase())throw new Error('Excel cell selection was not confirmed');}
  async function excelSnapshot(task){
    const id=task.id;
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
    const actual=formula();if(actual!==p.id)throw new Error('Excel row ID does not match. Refusing to edit another row.');
    await gotoCell(id,p.cell);
    const before=formula();
    if(!/^(TRUE|FALSE|ĐÚNG|SAI|0|1)$/i.test(before||''))throw new Error('Cannot read checkbox boolean; refusing to toggle blindly');
    if(!/^(TRUE|ĐÚNG|1)$/i.test(before)){
      // Trusted keystrokes are dispatched only to this bot-owned tab through CDP.
      await L.key(id,'Space');
      await gotoCell(id,p.cell);
      if(!/^(TRUE|ĐÚNG|1)$/i.test(formula()||''))throw new Error('Excel checkbox change was not confirmed');
    }
    // Wait for an actual saved state instead of assuming a successful click means cloud persistence.
    await L.until(()=>L.all('[aria-label],[title],[role="status"]').some(el=>L.visible(el)&&/^(saved|all changes saved|đã lưu|đã lưu tất cả thay đổi)(\b|$)/i.test(L.name(el))),30000);
    return {checked:true};
  }
  const host=location.hostname;
  const providerMap={image_pair:imagePair,flow_video:flowVideo,excel_snapshot:excelSnapshot,excel_tick:excelTick,
    flow_music:async task=>{const result=await globalThis.lofiProviders.flow_music(task);if(!result?.ok)throw new Error(result?.error||'Music generation failed');return {};}};
  function capabilities(){
    if(host==='chatgpt.com')return ['image_pair'];
    if(host==='flow.google.com')return ['flow_video'];
    if(host==='flowmusic.app'||host==='www.flowmusic.app')return ['flow_music'];
    if((host==='cmcuni-my.sharepoint.com'||host.endsWith('.officeapps.live.com'))&&(nameBox()||L.find(/^(file|tệp)$/i)))return ['excel_snapshot','excel_tick'];
    return [];
  }
  if(globalThis.__LOFI_PROVIDER_TEST__){globalThis.lofiProviderTest={providerMap,formula,gotoCell};return;}
  let busy=false;
  chrome.runtime.onMessage.addListener((m,_sender,reply)=>{
    if(m.type!=='run')return;
    if(busy||!capabilities().includes(m.task.kind)){reply({ok:false,error:'Wrong or busy frame'});return;}
    busy=true;reply({ok:true});
    (async()=>{
      try{const result=await providerMap[m.task.kind](m.task);await L.bridge('result',m.task.id,{ok:true,result});}
      catch(error){try{await L.bridge('diagnostic',m.task.id,{text:document.body.innerText.slice(-18000)});}catch{}try{await L.bridge('result',m.task.id,{ok:false,error:String(error)});}catch{}}
      finally{busy=false;}
    })();
  });
  const ready=()=>chrome.runtime.sendMessage({type:'ready',kinds:capabilities()}).catch(()=>{});
  ready();setInterval(ready,3000);
})();
