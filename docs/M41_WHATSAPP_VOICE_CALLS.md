# M41: Free WhatsApp voice calls where MAX talks

Status: implemented and unit-tested with fake audio; **not yet tried on a real Windows PC / WhatsApp call**.

"MAX, call Mom and tell her I'll be 20–30 minutes late" → `comms.whatsapp_call(to="Mom", message=...)`
(owner approves) → MAX opens WhatsApp Desktop, starts a voice call, waits for "hello", introduces herself as
an AI assistant calling for the owner, gives the message, chats, says goodbye, hangs up and puts a summary
in `comms.inbox` / `comms.call_status`. Cost: nothing (WhatsApp over Wi-Fi, local Whisper, local Piper voice).

## One-time setup (Windows)
1. Install **WhatsApp Desktop** (Microsoft Store) and log in.
2. Install **VB-Audio Virtual Cable** (free, vb-audio.com/Cable), reboot.
3. WhatsApp → Settings → Calls/Audio: **Microphone = "CABLE Output"**, Speaker = your normal speakers/headphones.
4. `pip install -e ".[calls]"` in MAX's `.venv`.
5. In `.env`: `MAX_WHATSAPP_CALLS=yes`, `MAX_OWNER_NAME=<your name>`, and set the hang-up shortcut (below).
6. Save the contact: "MAX, add Mom, phone +91 98765 43210".

## Shortcuts — check on your PC
WhatsApp Desktop shortcuts change between versions; press **Ctrl+/** in WhatsApp to see yours.
`MAX_WHATSAPP_CALL_KEYS` (default `ctrl+shift+c`, voice call) and `MAX_WHATSAPP_HANGUP_KEYS`
(no default — until it's set, MAX can't hang up; end the call yourself).

## Safety
Owner approves each call; only saved contacts; one call at a time; max 12 turns / 8 minutes; ends after
2 silent turns or no answer in 45 s. What the other person says only shapes MAX's spoken replies — it can't
run tools. MAX won't agree to payments, share codes or documents, or promise anything beyond the message.

## Limits
~1–3 s pause before each reply (listen → transcribe → think → speak). While a call runs, sound from other
apps on your speakers is also heard by MAX — keep other audio off. The mic of your PC is not used.
