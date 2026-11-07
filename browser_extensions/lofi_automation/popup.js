document.querySelector('#open').onclick=()=>chrome.tabs.create({url:LOFI_CONFIG.studio_url});
const status=document.querySelector('#status');
async function request(message){const r=await chrome.runtime.sendMessage(message);if(!r?.ok)throw new Error(r?.error||'Hãy chạy Studio trước');return r;}
async function refresh(){try{const r=await request({type:'profile-info'});status.textContent=`${r.role}: `+(r.active?'Đang được dùng':'Đã nối, chưa được chọn');document.querySelector('#name').value=r.instance.name;document.querySelector('#activate').disabled=r.active;}catch(e){status.textContent=e.message;}}
document.querySelector('#rename').onclick=async()=>{try{await request({type:'rename-profile',label:document.querySelector('#name').value});await refresh();}catch(e){status.textContent=e.message;}};
document.querySelector('#activate').onclick=async()=>{try{await request({type:'activate-profile'});await refresh();}catch(e){status.textContent=e.message;}};
refresh();
