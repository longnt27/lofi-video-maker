# Studio automation

Start the entire local tool:

```powershell
.\.venv\Scripts\python.exe lofi_studio.py --device cuda
```

Open <http://127.0.0.1:7860> and select **Tự động hóa**. Studio starts its
authenticated loopback browser bridge on port 7861. Use `--no-automation` for
the existing video-processing UI alone. No new Python dependencies are required.

## Local Excel workbook

Enter the workbook's full `.xlsx` path in **File Excel trên máy**.
Studio reads a fresh snapshot at the start of each scenario and writes the
completion checkbox directly to that same local file after rendering succeeds.
With a local path configured, both Excel steps run inside Studio.
The main Chrome profile is used for Google Flow and Flow Music.

The writer verifies the script ID in column A before changing column I.
It preserves the workbook's other ZIP contents, formatting and native checkbox
metadata, and stores the original under `cache/automation/assets/<task-id>/before.xlsx`
before replacing the file atomically. Close the workbook in Excel before the
completion step. If it is locked or its row ID changed, Studio stops and offers
the usual resume action.

The workbook source is pinned for each run. A new path applies to the next new
run. Leaving the path empty uses the existing Excel web workflow described below.

## One-time setup

- Install the generated `cache/automation/extensions/main` extension into the
  existing Chrome profile used for Excel, Google Flow and Flow Music (Profile 20
  on this machine). Install `cache/automation/extensions/gpt` into GPT-Plus
  (Profile 52). The UI opens the correct profile's extensions page for you.
- Chrome extensions page: Developer mode → Load unpacked → select the correct
  generated folder. Use the generated folders, not source templates; their
  configuration and private role keys are generated locally and ignored by Git.
- Keep both profiles open and signed in. Extension heartbeats show their connection.
- Configure the Excel workbook and ChatGPT project URL. Google Flow creates a
  new project for each scenario in the selected main Chrome profile.
  The ChatGPT project
  URL creates each scenario's image conversation inside that project. Its
  instructions/style.md stay in project context; the approved local reference
  images are also attached to the first image request. A local style.md is optional
  when the project is configured.
- In **Xử lý video**, upload one native Flow sample, draw a tight watermark box,
  and use **Lưu vùng watermark cho tự động hóa**. Its normalized coordinates work
  across the same output layout. The bot does not guess a broad inpainting region.
- Enter your Telegram bot token, chat ID and optional reviewer user ID in the
  local automation settings. Start a conversation with the bot first. In a private
  chat, the chat ID is also the default reviewer ID; groups require a reviewer ID.
  Tokens are never returned by the status API or committed to Git.

## Each approval cycle

1. Download a fresh workbook copy using the signed-in Excel browser UI.
2. Read the existing `Kich_ban`, `Prompts` and `YouTube` sheets. Select the first
   unchecked, non-skipped `LOFI-*` ID. The XLSX reader never rewrites the workbook.
3. Open a fresh conversation in the configured ChatGPT project. Attach the style
   images and optional local style.md, send the first image prompt, and transfer the resulting image to Studio.
4. Create a new Google Flow project and save its URL for this scenario. Upload
   the image there. Retries reuse the saved project. Select the configured
   Omni 1.1 Flash model, Video/Ingredients, 16:9, 720p, x1 and 10 seconds. If a required option is absent,
   stop rather than silently substituting another model. Download Original size.
5. Create the configured number of Flow Music tracks from the workbook prompt
   in the configured session. The extension records the selected song link before
   opening the download menu so a retry can download that same take.
6. Run the existing `studio_pipeline.py`: LaMa → tiled Fallin → seamless loop →
   playlist mix, with source ambience at 10% by default. Only the final mix goes
   to the output folder. Studio keeps the source media and render cache until
   the final mix is saved and the Excel checkbox is verified.
7. Reopen the cloud workbook, select the script sheet, verify the ID in column A,
   and check the corresponding column-I native checkbox only if currently FALSE.
   Verify TRUE and the saved state before reporting completion.
   The scenario is marked complete directly upon successful Excel tick.

For a comprehensive step-by-step walkthrough in Vietnamese, see [AUTOMATION_GUIDE.md](AUTOMATION_GUIDE.md).

The default full output duration follows the generated playlist. Set a preview
duration for testing or increase music count for a longer soundtrack. No YouTube
upload/publish is performed; metadata is saved under each scenario's cache folder.

## Working while it runs

### Change accounts when a plan expires

Chrome profiles are selectable by their display name/email in the automation UI;
you do not need to edit code or remember the directory number. Each extension
installation has a separate persistent identifier in that Chrome profile.

1. Pause if the role is processing a browser task. At the Telegram review gate,
   or while only the other role is busy, an idle role can be switched without
   cancelling the completed output or invalidating its Telegram approval.
2. Select the new Chrome profile by name. Save settings or click its Open Chrome
   button, which saves the selected profile before opening its extensions page.
3. Load the same `extensions/main` or `extensions/gpt` folder in the new profile.
   Open the extension popup, give it a recognizable name and click **Dùng profile này**.
   Alternatively choose its installation in Studio and activate it there.
4. Sign in to the needed services in the new profile. Make sure it can access
   the configured ChatGPT project and the generation services. Update the ChatGPT
   project URL if needed. Each scenario creates its own Flow project.
   Continue the paused step, or approve the pending result on Telegram as normal.

Only the selected installation for each role receives/claims tasks. An old profile
may remain open with the extension installed; its heartbeats cannot reactivate it.
New installations do not take over automatically after the first pairing. Token,
metadata, prompts, approval state and completed assets are preserved across switches.

### Background browser operation

All browser tasks use newly created, inactive tabs. DOM actions and trusted CDP
keystrokes target only those tabs through the extension's Debugger permission.
They do not use OS mouse/keyboard input, global shortcuts or the clipboard. You
can use other tabs and apps; avoid editing or closing the bot's own active task tab.
Chrome must stay running and the machine must remain awake. GPU encoding/upscaling
can still compete with other GPU-heavy work.

The main extension is limited to Flow Music, Google Flow, the specified SharePoint
tenant, Excel's officeapps iframe hosts and loopback. The GPT extension is limited
to ChatGPT and loopback. Debugger is a powerful Chrome permission; code binds it
only to a tab created for the current task and detaches afterward. Downloads
permission names/captures the new task download, not previous downloads.
The download URL must be confirmed by a DevTools download event from the bot's
own tab before it is renamed or copied. An origin-only referrer is not sufficient.

## Failures and recovery

The queue, task submissions and approval gate are durable local JSON. A restart
marks in-flight tasks interrupted; it never automatically replays a submitted
generation. Resume can retry read/tick operations, reuse validated downloads and
resume after rendering. A submitted generative request requires an explicit
resubmission checkbox before retrying. Cancel stops local automation, but a request
already submitted to a remote generator may still finish normally.

If a Flow Music download fails, click **Tiếp tục bước đang dừng** to collect
the recorded song. For older tasks without a recorded link, open the matching
take in the original session, copy its song link, and paste it under **Khôi phục
khi lỗi dịch vụ** before continuing. Reload the installed main extension on
Chrome's extensions page after updating the bridge and extension code.

After verifying the Excel checkbox, Studio removes the scenario's downloaded
images, clips, tracks and temporary render media. The output folder contains
`mix.mp4`. Studio retries cleanup if a file is open and keeps the scenario
materials available while a failed run can still be resumed.

## Validation status

Local tests cover the approval boundary, wrong/replayed Telegram callbacks,
profile-role isolation, no duplicate claims, restart behavior, workbook parsing,
audio validation, single-prompt image transfer in a Chrome DOM fixture, safe/idempotent
Excel-checkbox behavior in a DOM fixture, and existing media pipeline regressions.

Authenticated live selectors for Excel, the project's ChatGPT composer, Google Flow
and Flow Music still need an end-to-end account test after both extensions and
Telegram are configured. Fixture tests do not prove those current website layouts.
