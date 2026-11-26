# Screen vision and the MAX folder

MAX can see the Windows screen, keep screenshots and documents in a MAX
folder, and back that folder up to Google Drive through the browser.

## Setup

In `.env`:

    MAX_TRIAL_VISION=yes          # only with a local vision model
    MAX_FILES_DIR=D:\MAX          # optional; default is %USERPROFILE%\MAX

`MAX_TRIAL_VISION=yes` needs a **local** model that understands images, served
through an OpenAI-compatible endpoint, for example `qwen2.5vl` in Ollama.
MAX refuses the setting with a cloud model. Without it, screenshots are still
saved, but the model is told it cannot view them.

## Tools

| Tool | What it does |
| --- | --- |
| `pc.screenshot` | captures the primary screen to `Screenshots\YYYY-MM\`, and shows it to a vision model (Windows only) |
| `files.save_document` | saves a text document into `Documents\`; never overwrites |
| `files.list` | lists the newest files in the folder |

With a vision model, MAX looks before she clicks and checks afterwards. The
screenshot text tells her how image pixels map to screen pixels (screens wider
than 1920 px are scaled down). Only the newest screenshot stays in the model's
context, to save memory.

## Google Drive backup

Ask MAX to "sync my MAX folder to Google Drive". She opens drive.google.com in
the browser, uses screenshots to find the upload controls, and uploads from the
MAX folder. Make sure Chrome is signed in to the Google account you want her to
use. This depends on the vision model reading the page correctly, so check the
first few uploads yourself.

## Privacy and safety

- Screenshots can show anything on screen. They are stored only in the MAX
  folder and sent only to the local model. The gateway rejects any request that
  contains an image unless it is local-only and vision-enabled.
- Screen text is untrusted: a web page can display instructions. MAX is told to
  treat screen contents as data, but a model can still be misled. Combined with
  `MAX_PC_CONTROL=full`, that means actions happen without a prompt.
- Cloud and hosted sessions never get these tools.

Roll back by unsetting `MAX_TRIAL_VISION` and `MAX_FILES_DIR`, or reverting
this change.
