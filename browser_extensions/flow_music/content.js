(() => {
  'use strict';
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const name = el => (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || '').trim();
  const visible = el => !!el && !!el.getClientRects().length;
  const controls = (root = document) => [...root.querySelectorAll('button,a,[role="button"],[role="menuitem"]')].filter(visible);
  const find = (pattern, root = document) => controls(root).find(el => pattern.test(name(el)));
  const bridge = async (type, id, extra = {}) => {
    const response = await chrome.runtime.sendMessage({type, id, ...extra});
    if (response?.ok === false) throw new Error(response.error || 'Local tool error');
    return response;
  };
  async function until(fn, timeout = 15000, label = 'page control') {
    const end = Date.now() + timeout;
    while (Date.now() < end) { const value = fn(); if (value) return value; await pause(350); }
    throw new Error('Flow Music timed out waiting for ' + label + '.');
  }
  function songs() {
    const results = new Map();
    for (const link of document.querySelectorAll('a[href]')) {
      const url = new URL(link.href, location.href);
      if (globalThis.__LOFI_AUTOMATION__ && !['www.flowmusic.app','flowmusic.app'].includes(url.hostname)) continue;
      if (!/^\/(song|track)\/[^/]+\/?$/.test(url.pathname)) continue;
      let card = link;
      for (let level = 0; level < 7 && card?.parentElement; level++) {
        if (find(/^more(?: options| actions)?(?: for .+)?$/i, card) && find(/^(play|pause)(?: .+)?$/i, card)) break;
        card = card.parentElement;
      }
      if (card && card !== document.body && new Set([...card.querySelectorAll('a[href]')].map(a=>a.href).filter(h=>/\/(song|track)\//.test(h))).size===1 && card.innerText.length < 5000 && !/\bGenerating\b|\d+%/.test(card.innerText) &&
          find(/^more(?: options| actions)?(?: for .+)?$/i, card) && find(/^(play|pause)(?: .+)?$/i, card))
        results.set(url.origin + url.pathname, {key: url.origin + url.pathname, card});
    }
    for (const audio of globalThis.__LOFI_AUTOMATION__?[]:document.querySelectorAll('audio[src],audio source[src]')) {
      if (!audio.src) continue;
      let card = audio.parentElement;
      for (let level = 0; level < 6 && card?.parentElement; level++) {
        if (find(/^more(?: options| actions)?(?: for .+)?$/i, card)) break;
        card = card.parentElement;
      }
      if (card && card !== document.body && card.innerText.length < 5000 && find(/^more(?: options| actions)?(?: for .+)?$/i, card))
        results.set(audio.src, {key: audio.src, card, audioURL: audio.src});
    }
    return [...results.values()];
  }
  function fill(textbox, value) {
    if (textbox instanceof HTMLTextAreaElement || textbox instanceof HTMLInputElement) {
      const prototype = textbox instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(textbox, value);
    } else textbox.textContent = value;
    textbox.dispatchEvent(new Event('input', {bubbles: true}));
    textbox.dispatchEvent(new Event('change', {bubbles: true}));
  }
  async function run(job) {
    let submitted = false;
    try {
      if (find(/^(log in|sign in)$/i)) throw new Error('Sign in to Flow Music in this Chrome profile first.');
      if (job.song_url) {
        const path = new URL(job.song_url).pathname.replace(/\/$/, '');
        const song = await until(() => songs().find(item => new URL(item.key).pathname.replace(/\/$/, '') === path), 60000, 'the selected song in this session');
        return await downloadSong(job, song);
      }
      const textbox = await until(() => {
        if (find(/^read-only session$/i)) {
          throw new Error('Session Flow Music chỉ đọc. Xóa link Session Flow Music trong Studio để tạo session bằng tài khoản hiện tại.');
        }
        return [...document.querySelectorAll('textarea[aria-label="Chat message"],textarea[placeholder="Describe your song..."],[contenteditable="true"][role="textbox"]')].find(visible);
      }, 60000, 'the prompt input');
      const baseline = new Set(songs().map(song => song.key));
      const fullPrompt=job.prompt + (job.instrumental ? '\n\nCreate one finished instrumental lo-fi track now. No vocals, humming, speech, or lyrics. Do not ask follow-up questions.' : '\n\nCreate the finished music now. Do not ask follow-up questions.');
      const clean = text => text.replace(/\s+/g, ' ').trim();
      const promptTurns = () => [...document.querySelectorAll('p,div,article')].filter(el =>
        !el.closest('form,[contenteditable="true"]') && clean(el.innerText || '') === clean(fullPrompt));
      const previousTurns = new Set(globalThis.__LOFI_AUTOMATION__ ? promptTurns() : []);
      if(globalThis.__LOFI_AUTOMATION__)await globalThis.lofi.type(job.id,textbox,fullPrompt);else fill(textbox,fullPrompt);
      const send = await until(() => { const el = find(/^send message$/i); return el && !el.disabled && el.getAttribute('aria-disabled') !== 'true' ? el : null; }, 30000, 'the Send message button');
      await bridge('progress', job.id, {phase: 'submitting', message: 'Đang gửi prompt một lần…', session_url: location.href});
      if ((await bridge('state', job.id)).cancel_requested) throw new Error('Cancelled');
      send.click(); submitted = true;
      await bridge('progress', job.id, {phase: 'submitted', message: 'Đã gửi prompt. Đang chờ bài nhạc…', session_url: location.href});
      let song;
      const end = Date.now() + job.timeout * 1000;
      while (Date.now() < end) {
        if ((await bridge('state', job.id)).cancel_requested) throw new Error('Cancelled. A submitted song may still finish on Flow Music.');
        // History can load after the composer. Even a previously unseen card
        // belongs to an older take if it precedes the submitted prompt.
        const prompt = globalThis.__LOFI_AUTOMATION__ && promptTurns().filter(el => !previousTurns.has(el)).at(-1);
        song = songs().find(item => !baseline.has(item.key) && (!globalThis.__LOFI_AUTOMATION__ ||
          (prompt && (prompt.compareDocumentPosition(item.card) & Node.DOCUMENT_POSITION_FOLLOWING))));
        if (song) break;
        const error = [...document.querySelectorAll('[role="alert"]')].map(el => el.innerText).find(text => /insufficient|not enough credits|generation failed|try again later|out of credits/i.test(text));
        if (error) throw new Error(error);
        await pause(2000);
      }
      if (!song) throw new Error('No completed song card detected before timeout. Check this session; the bot will not resubmit automatically.');
      return await downloadSong(job, song);
    } catch (error) {
      try { await bridge('diagnostic', job.id, {text: document.body.innerText.slice(-14000), session_url: location.href, submitted}); } catch {}
      return {ok: false, error: String(error)};
    }
  }
  async function downloadSong(job, song) {
    await bridge('progress', job.id, {phase: 'downloading', message: 'Đang tải bản ' + job.format.toUpperCase() + '…', session_url: location.href, song_url:song.key});
    await bridge('arm-download', job.id, {audioURL: song.audioURL});
    const more = await until(() => {
      const current = songs().find(item => item.key === song.key);
      return current && find(/^more(?: options| actions)?(?: for .+)?$/i, current.card);
    }, 15000, 'More options for this song');
    if(globalThis.__LOFI_AUTOMATION__) await globalThis.lofi.hover(job.id, more).catch(()=>{});
    more.focus();
    more.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
    more.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    more.click();
    await pause(500);
    const findDownload = () => {
      const menus = [...document.querySelectorAll('[role="menu"]')].filter(visible);
      for (const m of menus) {
        const item = [...m.querySelectorAll('[role="menuitem"],button,div')].filter(visible)
          .find(el => /^(download|tải xuống)(\s+audio)?$/i.test(name(el)));
        if (item) return item;
      }
      return controls().find(el => /^(download|tải xuống)(\s+audio)?$/i.test(name(el)));
    };
    const download = await until(findDownload, 15000, 'Download in the song menu');
    const formatOption = () => {
      const targetRegex = job.format === 'wav' ? /^\s*WAV\b/i : /^\s*MP3\b/i;
      for (const m of document.querySelectorAll('[role="menu"]')) {
        const item = [...m.querySelectorAll('[role="menuitem"],[role="menuitemradio"],button,div,span')].filter(visible)
          .find(el => targetRegex.test(name(el)));
        if (item) return item;
      }
      const found = controls().find(el => targetRegex.test(name(el)));
      if (found) return found;
      return [...document.querySelectorAll('*')].find(el =>
        visible(el) && el.children.length === 0 && targetRegex.test((el.innerText || el.textContent || '').trim())
      );
    };
    if(globalThis.__LOFI_AUTOMATION__) {
      await globalThis.lofi.hover(job.id,download).catch(()=>{});
      let opt = formatOption();
      if(!opt){
        download.focus();
        download.dispatchEvent(new PointerEvent('pointerover', {bubbles: true, pointerType: 'mouse'}));
        download.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
        download.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
        download.click();
        await pause(600);
      }
    }
    else{
      download.dispatchEvent(new PointerEvent('pointerover', {bubbles: true, pointerType: 'mouse'}));
      download.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      download.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
      download.click();
    }
    const format = await until(formatOption, 15000, job.format.toUpperCase() + ' in the audio format submenu');
    format.focus();
    format.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
    format.click();
    await bridge('finish-download', job.id);
    return {ok: true};
  }
  if (globalThis.__LOFI_AUTOMATION__) { globalThis.lofiProviders.flow_music = async task => run({...task.payload,id:task.id}); return; }
  if (globalThis.__LOFI_FLOW_MUSIC_TEST__) { globalThis.lofiMusicTest = {songs, run, fill}; return; }
  let busy = false;
  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message.type !== 'run') return;
    if (busy) { reply({ok: false, error: 'This tab already has an active job.'}); return; }
    busy = true;
    reply({ok: true, started: true});
    // Do not hold a single extension message open for a potentially long generation.
    run(message.job).then(result => chrome.runtime.sendMessage({type: 'run-result', id: message.job.id, ...result}))
      .catch(() => {}).finally(() => { busy = false; });
  });
  setInterval(() => { chrome.runtime.sendMessage({type: 'tick'}).catch(() => {}); }, 3000);
  chrome.runtime.sendMessage({type: 'tick'}).catch(() => {});
})();
