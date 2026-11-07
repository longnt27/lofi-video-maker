importScripts('config.js');
let running = false;
let watch = null;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const musicURL = value => {
  try { const u = new URL(value); return u.protocol === 'https:' && ['www.flowmusic.app', 'flowmusic.app'].includes(u.hostname); }
  catch { return false; }
};
async function api(path, body) {
  const response = await fetch(LOFI_BRIDGE.url + '/bridge/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Authorization': `Bearer ${LOFI_BRIDGE.token}`, 'Content-Type': 'application/json'},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: AbortSignal.timeout(15000)
  });
  if (!response.ok) throw new Error(`Local tool: ${response.status} ${await response.text()}`);
  return response.json();
}
function matches(item) {
  return watch && (item.referrer?.split('#')[0] === watch.sessionURL.split('#')[0] ||
    (watch.audioURL && item.url === watch.audioURL) ||
    item.url?.startsWith('blob:https://www.flowmusic.app/') || item.url?.startsWith('blob:https://flowmusic.app/'));
}
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  const ext = (item.filename.match(/\.(mp3|wav|flac|m4a|ogg)$/i) || [])[1];
  if (matches(item) && ext) suggest({filename: `LoFiFlowMusic/${watch.jobId}/track.${ext.toLowerCase()}`, conflictAction: 'uniquify'});
  else suggest();
});
chrome.downloads.onCreated.addListener(item => { if (matches(item) && watch.id === undefined) watch.id = item.id; });
async function finishDownload(jobId) {
  if (!watch || watch.jobId !== jobId) throw new Error('No active download');
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    if ((await api('state', {id: jobId})).cancel_requested) throw new Error('Cancelled');
    if (watch.id !== undefined) {
      const [item] = await chrome.downloads.search({id: watch.id});
      if (item?.state === 'interrupted') throw new Error('Chrome download interrupted: ' + item.error);
      if (item?.state === 'complete') return api('download', {id: jobId, source: item.filename});
    }
    await pause(1000);
  }
  throw new Error('Download timed out. Check the song menu and Chrome downloads.');
}
async function poll() {
  if (running) return {ok: true};
  running = true;
  let job;
  let handedOff = false;
  let claimed = false;
  try {
    const pending = await api('next');
    if (!pending.job) return {ok: true};
    job = pending.job;
    await api('claim', {id: job.id});
    claimed = true;
    const tab = await chrome.tabs.create({url: 'https://www.flowmusic.app/session?t=true', active: true});
    await chrome.storage.session.set({activeJob: job.id, activeTab: tab.id});
    await api('progress', {id: job.id, phase: 'opening', message: 'Đang mở phiên Flow Music mới…'});
    let response;
    for (let retry = 0; retry < 45; retry++) {
      const current = await chrome.tabs.get(tab.id);
      if (current.status === 'complete') {
        if (!musicURL(current.url)) throw new Error('Sign in to Flow Music in this Chrome profile, then create a new job.');
        try { response = await chrome.tabs.sendMessage(tab.id, {type: 'run', job}); break; }
        catch (error) { if (!/Receiving end does not exist|Could not establish connection/.test(String(error))) throw error; }
      }
      await pause(1000);
    }
    if (!response) throw new Error('Cannot connect to the Flow Music tab. Check login or reload the extension.');
    if (!response.ok) throw new Error(response.error || 'Flow Music failed');
    handedOff = true;
    return {ok: true};
  } catch (error) {
    if (claimed) { try { await api('fail', {id: job.id, message: String(error)}); } catch {} }
    return {ok: false, error: String(error)};
  } finally {
    if (!handedOff) {
      if (claimed) await chrome.storage.session.remove(['activeJob', 'activeTab']);
      watch = null; running = false;
    }
  }
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const action = async () => {
    if (message.type === 'tick') return poll();
    if (!sender.tab || !musicURL(sender.url)) throw new Error('Only Flow Music tabs can control generation');
    if (!['progress', 'state', 'arm-download', 'finish-download', 'diagnostic', 'run-result'].includes(message.type)) throw new Error('Unknown message');
    const {activeJob} = await chrome.storage.session.get('activeJob');
    if (activeJob !== message.id) throw new Error('Job is not active in this extension');
    if (message.type === 'run-result') {
      try {
        return await api(message.ok ? 'done' : 'fail', {id: message.id, message: message.error || ''});
      } finally {
        await chrome.storage.session.remove(['activeJob', 'activeTab']);
        watch = null; running = false;
      }
    }
    if (message.type === 'arm-download') { watch = {jobId: message.id, sessionURL: sender.url, audioURL: message.audioURL}; return {ok: true}; }
    if (message.type === 'finish-download') return finishDownload(message.id);
    return api(message.type, message);
  };
  action().then(reply).catch(error => reply({ok: false, error: String(error)}));
  return true;
});
chrome.alarms.onAlarm.addListener(() => { void poll(); });
chrome.tabs.onRemoved.addListener(async tabId => {
  const {activeJob, activeTab} = await chrome.storage.session.get(['activeJob', 'activeTab']);
  if (activeJob && activeTab === tabId) {
    try { await api('fail', {id: activeJob, message: 'Flow Music tab closed. Check the session before generating again.'}); } catch {}
    await chrome.storage.session.remove(['activeJob', 'activeTab']); watch = null; running = false;
  }
});
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create('lofi-bridge', {periodInMinutes: 0.5}); void poll(); });
chrome.runtime.onStartup.addListener(async () => {
  const {activeJob} = await chrome.storage.session.get('activeJob');
  if (activeJob) { try { await api('fail', {id: activeJob, message: 'Chrome restarted. Check the saved session before submitting again.'}); } catch {} }
  chrome.alarms.create('lofi-bridge', {periodInMinutes: 0.5}); void poll();
});
