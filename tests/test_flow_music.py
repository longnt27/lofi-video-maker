"""Local controller, audio validation and actual browser DOM adapter checks."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from flow_music import create_app
from flow_music_browser import ROOT, chrome_executable


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'controller')
    with TestClient(app, base_url='http://127.0.0.1:7861') as client:
        client.ui = {'X-LoFi-Token': app.state.store.token}
        client.bridge = {'Authorization': 'Bearer ' + app.state.store.token}
        client.output = tmp_path / 'music'
        yield client


def queue(client):
    r = client.post('/api/jobs', headers=client.ui, json={
        'prompt': 'Soft instrumental lo-fi for a quiet summer afternoon.',
        'title': 'Quiet afternoon', 'output_dir': str(client.output)})
    assert r.status_code == 200, r.text
    return r.json()


def event(client, path, job, **extra):
    return client.post('/bridge/' + path, headers=client.bridge, json={'id': job['id'], **extra})


def test_local_access_and_single_claim(client):
    assert client.get('/bridge/next').status_code == 403
    assert client.post('/api/jobs', json={'prompt': 'hello there'}).status_code == 403
    assert client.get('/api/status', headers={'Origin': 'https://example.com'}).status_code == 403
    assert client.get('/api/status', headers={'Host': 'attacker.example'}).status_code == 400
    job = queue(client)
    assert queue_request(client).status_code == 409
    assert event(client, 'claim', job).status_code == 200
    assert event(client, 'claim', job).status_code == 409
    assert event(client, 'done', job).status_code == 409
    assert event(client, 'progress', job, phase='submitted', session_url='https://example.com/session/x').status_code == 400


def queue_request(client):
    return client.post('/api/jobs', headers=client.ui,
                       json={'prompt': 'Another lo-fi song', 'output_dir': str(client.output)})


def test_cancel_before_submission_and_restart_never_resubmits(client):
    job = queue(client)
    client.post('/api/cancel', headers=client.ui, json={'id': job['id']})
    assert client.get('/bridge/next', headers=client.bridge).json()['job'] is None
    second = queue(client)
    event(client, 'claim', second)
    event(client, 'progress', second, phase='submitted', session_url='https://www.flowmusic.app/session/test')
    app = create_app(client.app.state.store.home)
    saved = app.state.store.get(second['id'])
    assert saved['status'] == 'interrupted'
    assert saved['submitted']
    assert saved['session_url'].endswith('/session/test')


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
def test_only_job_download_is_validated_and_saved_without_overwrite(client, tmp_path):
    job = queue(client)
    event(client, 'claim', job)
    event(client, 'progress', job, phase='downloading')
    unrelated = tmp_path / 'old-song.mp3'
    unrelated.write_bytes(b'preserve user file')
    assert event(client, 'download', job, source=str(unrelated)).status_code == 400
    assert unrelated.read_bytes() == b'preserve user file'
    source = tmp_path / 'Downloads/LoFiFlowMusic' / job['id'] / 'track.mp3'
    source.parent.mkdir(parents=True)
    source.write_text('<html>not audio</html>')
    assert event(client, 'download', job, source=str(source)).status_code == 400
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.3', str(source)], check=True)
    result = event(client, 'download', job, source=str(source))
    assert result.status_code == 200, result.text
    output = Path(result.json()['output'])
    assert output.parent == client.output
    assert output.read_bytes() == source.read_bytes()
    assert event(client, 'download', job, source=str(source)).status_code == 409
    assert event(client, 'done', job).status_code == 200
    assert source.exists()  # Never remove old or downloaded user files.


def test_browser_adapter_sends_once_ignores_old_song_and_downloads_mp3(tmp_path):
    from playwright.sync_api import sync_playwright
    try:
        chrome = chrome_executable()
    except FileNotFoundError:
        pytest.skip('Chrome required for actual DOM adapter check')
    script = (ROOT / 'browser_extensions/flow_music/content.js').read_text(encoding='utf-8')
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome, headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<textarea aria-label="Chat message"></textarea>
              <button aria-label="Send message">Send</button><div id="cards">
              <article><a href="https://www.flowmusic.app/song/old">Old song</a>
              <button aria-label="Play"></button><button aria-label="More"></button></article></div>''')
            page.evaluate('''() => {
                window.__LOFI_FLOW_MUSIC_TEST__ = true;
                window.events = []; window.sends = 0; window.exported = false;
                window.chrome = {runtime:{sendMessage: async m => {
                    events.push(m);
                    if(m.type === 'finish-download' && !exported) return {ok:false,error:'Format not selected'};
                    return {ok:true,cancel_requested:false};
                }}};
                document.querySelector('[aria-label="Send message"]').onclick = () => {
                    sends++;
                    const card = document.createElement('article');
                    card.innerHTML = '<a href="https://www.flowmusic.app/song/new">New song</a><button aria-label="Play"></button><button aria-label="More">more</button>';
                    card.querySelector('[aria-label="More"]').onclick = () => {
                        const d=document.createElement('button');d.setAttribute('role','menuitem');d.textContent='Download';
                        d.onclick=()=>{const mp3=document.createElement('button');mp3.textContent='MP3';mp3.onclick=()=>{exported=true;};document.body.append(mp3);};
                        document.body.append(d);
                    };
                    document.querySelector('#cards').append(card);
                };
            }''')
            page.add_script_tag(content=script)
            result = page.evaluate("lofiMusicTest.run({id:'test',prompt:'Warm piano',instrumental:true,timeout:30,format:'mp3'})")
            assert result == {'ok': True}, result
            assert page.evaluate('sends') == 1
            assert page.evaluate('exported')
            assert 'No vocals' in page.locator('textarea').input_value()
            assert page.evaluate("events.filter(e=>e.type==='progress').map(e=>e.phase)") == ['submitting', 'submitted', 'downloading']
            # Pre-submit cancellation must not consume another generation.
            page.evaluate("chrome.runtime.sendMessage = async () => ({ok:true,cancel_requested:true})")
            result = page.evaluate("lofiMusicTest.run({id:'cancel',prompt:'Do not submit this',instrumental:true,timeout:30,format:'mp3'})")
            assert not result['ok']
            assert page.evaluate('sends') == 1
        finally:
            browser.close()
