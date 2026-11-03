document.querySelector('#open').onclick = () => chrome.tabs.create({url: LOFI_BRIDGE.url});
chrome.runtime.sendMessage({type: 'tick'}).then(r => {
  document.querySelector('#state').textContent = r?.ok ? 'Đã kết nối công cụ local.' : 'Chưa kết nối. Hãy chạy flow_music.py.';
}).catch(() => { document.querySelector('#state').textContent = 'Hãy chạy flow_music.py trước.'; });
