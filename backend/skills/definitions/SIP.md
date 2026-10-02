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

## Interactive calls

1. `sip_call(action="plan", to=...)` — validates the account and the number and
   returns a redacted plan. Nothing is dialled. Show the operator what you
   intend to do and wait for them to agree.
2. `sip_call(action="call", to=..., text=..., confirm=true)` — only after they
   have said yes.

A `call` without `confirm=true` will not dial during an interactive chat; ask
first. Do not retry it in a loop hoping a different phrasing works. Approval is
for the number you showed them, not for calling whoever you like afterwards.

When a scheduled task explicitly instructs you to call, that task is the
operator's authorization: do not ask for another confirmation. For a scheduled
notification, call `sip_call(action="call", to="me", text=...)`. The configured
call-me destination is used, the message is spoken, and the tool ends the
one-way notification call. If the call-me destination is not configured, report
that setting is needed; never guess it.

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
- Repeat for as many exchanges as the conversation needs. Do not hang up merely
  because one answer, or two, has arrived. A normal exchange is not an end signal.

End an interactive call only when the caller clearly says they are finished, the
operator asks to end it, `listen` reports that the caller hung up, or the call's
time limit is reached. Then hang up if the session is still live and report the
transcript. If a tool reports the caller hung up, do not try to reuse that
session. A one-way scheduled notification is the exception: it ends after its
message is spoken.

`heard_nothing: true` means they said nothing intelligible — silence at the start
of a question is normal, they may still be thinking. Wait and listen again rather
than filling the gap with your own words.

## Numbers

Never invent, guess, complete or look up a phone number. Use only a number the
operator gave you in this conversation, or one they have already used with this
tool. If you do not have a number, ask for one. A wrong number reaches a
stranger who cannot consent to the call.

### Finding their number

For `to="me"` or `to="operator"`, SIP uses the explicit call-me number saved in
SIP settings. This is the required destination for scheduled notifications. If
it is not set, do not substitute the SIP account username or guess; tell the
operator to configure the number.

For an interactive chat, if no call-me number is configured, memory may be used
to look up a number the operator previously identified as their own ("my phone
number", "how do you reach me").

Only ever dial a number memory attributes to **the operator themselves** — a
fact they explicitly asked you to remember, or a number they have already used
with this tool. Memory also holds phone numbers lifted out of documents,
contact lists, invoices and directories they have merely had on this machine.
Those are somebody else's number. Dialling one calls a stranger who never
agreed to hear from anyone, so if the number you find belongs to a person,
firm or document rather than to the operator, ask instead of calling.

If `recall` is unavailable or has nothing of the operator's own, ask for the
number during an interactive chat. A scheduled task cannot ask, so report the
missing SIP call-me setting instead. Never fall back to a number from memory
that is not theirs.

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