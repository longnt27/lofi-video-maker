document.querySelector('#open').onclick=()=>chrome.tabs.create({url:LOFI_CONFIG.studio_url});
chrome.runtime.sendMessage({type:'tick'}).then(r=>document.querySelector('#status').textContent=`Profile ${LOFI_CONFIG.role}: `+(r?.ok?'Đã kết nối':'Hãy chạy Studio')).catch(()=>document.querySelector('#status').textContent='Hãy chạy Studio');
