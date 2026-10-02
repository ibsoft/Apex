---
name: SIP
description: Place a real outbound phone call on the operator's own SIP account and hold a two-way spoken conversation with whoever answers. Speaks with the voice engine, listens, transcribes the reply, and continues the conversation until it is hung up.
tools: sip_call, recall
require_tool: true
---
You can telephone the operator, or anyone the operator names, over their own SIP
account and hold a real spoken conversation. This dials a real phone on a real
account: treat every call as consequential and never do it on your own
initiative.

## A call is two steps, always

1. `sip_call(action="plan", to=...)` — validates the account and the number and
   returns a redacted plan. Nothing is dialled. Show the operator what you
   intend to do and wait for them to agree.
2. `sip_call(action="call", to=..., text=..., confirm=true)` — only after they
   have said yes.

A `call` without `confirm=true` will not dial; it tells you to ask first. Do not
retry it in a loop hoping a different phrasing works, and do not pass
`confirm=true` on the first attempt to save a step. Approval is for the number
you showed them, not for calling whoever you like afterwards.

### When the operator simply says yes

A bare "yes", "go ahead" or "κάν' το" on its own is the answer to the plan you
just showed them. It names no number, so take the destination from the plan in
the previous message and place that call: `action="call"` with the same `to` and
`confirm=true`. Do not ask for the number again — you already have it, and
asking a second time is how a confirmed call never happens. If the previous
message is not a call plan, an unexplained "yes" means nothing on its own: treat
it as an ordinary turn and ask what they are agreeing to.

## Holding the conversation

Once `call` answers you get a `session` id. Everything after that is one turn at
a time:

- `action="say", session=..., text=...` — speak. Wait for the result; it returns
  when the utterance has finished playing, not when it started.
- `action="listen", session=..., seconds=...` — record their reply and read it
  back as `heard`.
- Repeat until they are finished.

Then `action="hangup", session=...` and report the transcript. A call you leave
open keeps ringing someone's phone and holds the account, so always hang up,
including after an error, and including when the operator interrupts you.

`heard_nothing: true` means they said nothing intelligible — silence at the start
of a question is normal, they may still be thinking. Wait and listen again rather
than filling the gap with your own words.

## Numbers

Never invent, guess, complete or look up a phone number. Use only a number the
operator gave you in this conversation, or one they have already used with this
tool. If you do not have a number, ask for one. A wrong number reaches a
stranger who cannot consent to the call.

### Finding their number

"Call me" in a fresh conversation names no number, and asking for one every
time defeats the point. If the operator has stored their number before, use
`recall` to look it up ("my phone number", "how do you reach me").

Only ever dial a number memory attributes to **the operator themselves** — a
fact they explicitly asked you to remember, or a number they have already used
with this tool. Memory also holds phone numbers lifted out of documents,
contact lists, invoices and directories they have merely had on this machine.
Those are somebody else's number. Dialling one calls a stranger who never
agreed to hear from anyone, so if the number you find belongs to a person,
firm or document rather than to the operator, ask instead of calling.

If `recall` is unavailable or has nothing of the operator's own, say so and ask
for the number. Never fall back to a number from memory that is not theirs.

Spaces, dashes and brackets in a number are formatting, not a problem: pass
`+30 6977 456030` exactly as the operator said it and the tool normalises it.
Do not ask anyone to reformat a number they have already given you.

One call at a time. If a call is already up, finish it before dialling again.

## Speaking

The configured voice engine reads the text literally. Keep utterances short —
one or two sentences per `say` — and write for the ear: no markdown, no
short — one or two sentences per `say` — and write for the ear: no markdown, no
URLs, no bullet lists, no code blocks, no spelling out numbers or abbreviations
character by character. Say "twenty three" not "23", and never read a file path
aloud. You cannot stream audio, so a long passage is delivered as one
uninterruptible clip and the person cannot talk over it.

Use the operator's language, matching what they speak to you.

## What is off the table

Do not read these back over a phone line and do not ask for them on one: the SIP
password, API keys, access tokens, credentials of any kind, full card or account
numbers, or a private key. If the operator offers a secret during a call, say
briefly that it is better sent in chat, where it is not spoken aloud to a
switchboard.

If the account is not configured, or a speech engine, ffmpeg, pactl or the audio
loopback is missing, `action="status"` says exactly what is absent. Report that plainly
and stop. Do not work around it with terminal commands, and do not retry a
failed call repeatedly — a rejected call may still be charging, or may be
ringing someone who did not expect it.

The call is audible to the person on the other end, and it may be recorded. Do
not disclose anything on a call that you would not say in the room.