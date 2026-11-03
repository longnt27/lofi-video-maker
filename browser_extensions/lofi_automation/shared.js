globalThis.__LOFI_AUTOMATION__=true;
globalThis.lofiProviders={};
globalThis.lofi={
  pause:ms=>new Promise(resolve=>setTimeout(resolve,ms)),
  all(selector,root=document){const out=[...root.querySelectorAll(selector)];for(const node of root.querySelectorAll('*'))if(node.shadowRoot)out.push(...this.all(selector,node.shadowRoot));return out;},
  visible:el=>!!el&&!!el.getClientRects().length,
  name:el=>(el.getAttribute('aria-label')||el.getAttribute('title')||el.innerText||'').trim(),
  find(pattern,root=document){return this.all('button,a,[role="button"],[role="menuitem"],[role="radio"],[role="tab"]',root).find(el=>this.visible(el)&&pattern.test(this.name(el)));},
  async until(fn,timeout=20000){const end=Date.now()+timeout;while(Date.now()<end){const value=fn();if(value)return value;await this.pause(500);}throw new Error('Expected page control did not appear');},
  async bridge(type,id,extra={}){const r=await chrome.runtime.sendMessage({type,id,...extra});if(r?.ok===false)throw new Error(r.error);return r;},
  fill(el,value){if(el instanceof HTMLInputElement||el instanceof HTMLTextAreaElement){Object.getOwnPropertyDescriptor(el instanceof HTMLInputElement?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,'value').set.call(el,value);}else el.textContent=value;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));},
  async key(id,key){return this.bridge('key',id,{key});},
  async text(id,text){return this.bridge('key',id,{text});},
  async type(id,el,text){el.focus();const focused=el.getRootNode().activeElement;
    if(focused!==el&&!el.contains(focused))throw new Error('Editor focus was not confirmed');
    await this.key(id,'ControlA');await this.text(id,text);},
  async upload(input,assets){const data=new DataTransfer();for(const a of assets){const bytes=Uint8Array.from(atob(a.base64),c=>c.charCodeAt(0));data.items.add(new File([bytes],a.name,{type:a.mime}));}input.files=data.files;input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));},
  async check(id){if((await this.bridge('state',id)).cancel_requested)throw new Error('Stopped');},
  async progress(id,message,submitted=false){return this.bridge('progress',id,{message,submitted});}
};
