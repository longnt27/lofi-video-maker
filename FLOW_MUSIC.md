# Flow Music local

For the integrated Excel → ChatGPT → Flow → music → mix → Telegram pipeline,
use Studio's **Tự động hóa** tab instead. See [AUTOMATION.md](AUTOMATION.md).
The standalone controller below is retained for isolated music tests.

Run from this checkout with the existing venv (no new packages required):

```powershell
.\.venv\Scripts\python.exe flow_music.py --open
```

Open <http://127.0.0.1:7861>. This is separate from the video Studio.

## Existing signed-in Chrome account

The tool opens the Chrome profile matching `--account`. It only reads Chrome's
profile names and email metadata to find that profile. It does not copy cookies,
passwords, history, or the profile directory. Other Chrome windows can stay open.
`FLOW_MUSIC_PROFILE="Profile 20"` can explicitly select a profile directory.

Chrome restricts remote debugging of its default user-data directory, so an
extension connects the existing profile to the local tool instead of CDP.

1. Start the local tool and click **Mở trang extension**.
2. In the correct Chrome profile, enable Developer mode, click Load unpacked,
   and select `D:\lofi-video-maker\cache\flow-music\extension`.
3. Open or reload <https://www.flowmusic.app/> in the same profile. Sign in
   manually if the Flow Music site needs it. Wait for the local connection indicator.
4. Enter a prompt, file title, output folder and MP3/WAV, then create one song.

The extension requests access to Flow Music, loopback HTTP and Chrome Downloads.
It does not request access to ChatGPT, Google Flow video or other website content.
It polls only the local controller and uses Flow Music's visible page controls.
Its bridge token is generated into the ignored `cache` folder, not source control.
Do not publish that generated extension folder or token.

## Outputs and generation behavior

- One click sends one prompt in a fresh session. It downloads the first detected
  completed song. It does not buy credits, publish songs or automatically resubmit
  a failed/ambiguous generation. Existing Flow Music credits/limits apply.
- MP3 is the default; WAV requires the format to be available in the account.
  No transcoding is used to disguise one format as another.
- New downloads are named `Downloads/LoFiFlowMusic/<job-id>/track.<format>` by Chrome.
  The local controller validates actual audio using FFprobe, then copies the track
  to the chosen folder, default `data/musics/flowmusic` on the project drive.
  The original Chrome download is retained. Existing files are not removed.
- Controller job metadata and failure page text are under `cache/flow-music/jobs`.
  The user's existing Chrome profile and its cache remain in their original location.
- Cancel stops local automation. An already-submitted remote generation may
  continue and consume its normal credits. The saved session link lets you inspect it.
- After a controller restart, an in-flight job is marked interrupted, never requeued.

## Validation and current limit

`python -m pytest tests/test_flow_music.py -q` checks local request isolation,
single claim/cancellation/restart, real audio validation without overwriting,
and the DOM adapter in actual Chrome against a synthetic page. It verifies exactly
one send, ignores an old song, selects MP3 and refuses a cancelled send.

The signed-in Flow Music result-card/download selectors require a live run after
loading the extension. They have not yet been verified against this user's account.
If the site's interface differs, the job fails with its session preserved and a
local page-text diagnostic instead of silently returning an unrelated old song.
