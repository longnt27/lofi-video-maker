"""Open the existing Chrome profile; never copy credentials or personal data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
HOME = ROOT / 'cache' / 'flow-music'
SITE = 'https://www.flowmusic.app/'
DEFAULT_ACCOUNT = 'dinhquocviet0411@gmail.com'


def chrome_profiles():
    """Only profile display metadata; never read cookies or credentials."""
    if sys.platform == 'win32':
        base=Path(os.environ['LOCALAPPDATA'])/'Google/Chrome/User Data'
    elif sys.platform == 'darwin':
        base=Path.home()/'Library/Application Support/Google/Chrome'
    else:
        base=Path.home()/'.config/google-chrome'
    try:
        info=json.loads((base/'Local State').read_text(encoding='utf-8')).get('profile',{}).get('info_cache',{})
        return [{'directory':folder,'name':value.get('name') or folder,'account':value.get('user_name') or ''}
                for folder,value in sorted(info.items(),key=lambda pair:pair[1].get('name','').casefold())]
    except (OSError,ValueError,KeyError):return []


def chrome_executable():
    candidates = [Path(os.environ['FLOW_MUSIC_CHROME'])] if os.environ.get('FLOW_MUSIC_CHROME') else []
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / 'Google/Chrome/Application/chrome.exe')
    candidates.append(Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    for name in ('google-chrome', 'google-chrome-stable', 'chrome', 'chromium'):
        if path := shutil.which(name):
            return path
    raise FileNotFoundError('Chrome not found. Set FLOW_MUSIC_CHROME to chrome.exe.')


def profile_directory(account=DEFAULT_ACCOUNT):
    if os.environ.get('FLOW_MUSIC_PROFILE'):
        return os.environ['FLOW_MUSIC_PROFILE']
    if sys.platform == 'win32':
        base = Path(os.environ['LOCALAPPDATA']) / 'Google/Chrome/User Data'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support/Google/Chrome'
    else:
        base = Path.home() / '.config/google-chrome'
    state = json.loads((base / 'Local State').read_text(encoding='utf-8'))
    matches = [folder for folder, info in state.get('profile', {}).get('info_cache', {}).items()
               if info.get('user_name', '').casefold() == account.casefold()]
    if len(matches) != 1:
        raise ValueError('Set FLOW_MUSIC_PROFILE to the correct Chrome profile directory.')
    return matches[0]


def open_chrome(url=SITE, account=DEFAULT_ACCOUNT):
    if url not in (SITE, SITE + 'session?t=true', 'chrome://extensions/'):
        raise ValueError('Unsupported Chrome destination')
    subprocess.Popen([chrome_executable(), '--profile-directory=' + profile_directory(account),
                      '--new-window', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
