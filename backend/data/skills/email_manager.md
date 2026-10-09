---
name: email_manager
description: Check, read and send POP3/SMTP email in the background through the pack script, and run a command to email its output. Read and sent history is kept in a local database; credentials stay in the pack .env and are never shown.
tools: run_shell
require_tool: true
exclude_tools: terminal_command, terminal_sessions
---

You operate the operator's mailbox through the email_manager pack. Every mailbox action runs the pack's single script, and it always runs in the background with the `run_shell` tool - never in a terminal window, never with `terminal_command`. The script's output is returned to you; you report it to the operator.

    run_shell: python3 /home/ioannisb/Development/Apex/backend/data/skills/email_manager/mail.py <command>

That directory is this skill's pack. Its `.env` is the only configuration source.

Commands
- `--check` - validate the .env and probe POP3 + SMTP with a real login (no mail is sent). Run it first whenever something fails, and when the operator asks whether email is set up.
- `check [--last N] [--all]` - inbox summary: message count, then the newest N headers with the server's own numbers (default 10, newest first). Messages already read are hidden; add `--all` to list every message, where read ones carry a `[read]` marker.
- `read <n> [--raw] [--full] [--body]` - one message: headers plus text body (HTML is converted to text when there is no plain part). Attachments are listed by name and size only, never printed. The body is capped at 3000 characters to fit the tool result; `--full` removes the cap. `--raw` prints the raw RFC822 source. `--body` prints only the body with signatures and quoted replies removed. Reading a message marks it read, so `check` will not list it again.
- `unread <n>` - mark a message unread again so `check` lists it.
- `send --to A [--to B] --subject S [--body TEXT | --body-file FILE | piped text] [--html] [--attach FILE]` - send one message. Confirm the recipient(s) and the subject with the operator first unless they were already explicit.
- `sent [--last N]` - list messages this script has sent, newest first, with the id to show each one (default 10).
- `sent-read <id> [--full]` - show one stored sent message: recipient(s), subject, date, body and attachment names.

Tracking and history
Read state and every sent message are kept in a small database (`mail.sqlite3`, mode 0600) next to the script; it holds no passwords. Use it: do not list a message again after it has been read, and when the operator asks what mail they have sent, use `sent` then `sent-read <id>` to recall its contents exactly.

Running a command and emailing its result
When the operator asks you to run a command and email the output (for example "run df -h and email me the result"), do it in one background command that pipes the output into the script:

    run_shell: df -h | python3 /home/ioannisb/Development/Apex/backend/data/skills/email_manager/mail.py send --to the@address --subject "df -h output"

The command runs in the background, so no terminal window opens. If the operator gave no subject, choose a short one that names the command. Do not paste the command output into chat unless asked - a short summary is enough. The message and its content are recorded in the sent history automatically. Run only the command the operator asked for; never a destructive or privileged command to fill an email.

How to run it
Call `run_shell` with the command above as the `command` argument and leave `on_screen` false. Do not use `terminal_command` in this skill: it opens a visible terminal window, which is exactly what the operator does not want for mail. A mail turn is only ever the script plus your summary - no window appears.

Listening to a message (voice)
When the operator is chatting by voice, "read my email" means summarise it, never recite it. Run `read <n> --body` (body text only, signatures and quoted replies already removed), then speak a 1-3 sentence summary: who it is from, the gist, and any action, deadline or figure the operator needs. Say that you are summarising. Never speak aloud headers, dates, addresses, attachment lists, signatures, legal footers, tracking text or quoted reply chains, and never read the body line by line. If the operator explicitly asks to hear the message read out, read only the `--body` text, still leaving out the signature and quoted part. Keep the summary in your spoken reply - do not paste the whole body.

Rules
1. The pack `.env` holds the credentials and is the one configuration source. Never print, quote, paraphrase, summarise or recall its contents - not the whole file, not one line, not from tool output, and not from earlier in the conversation. Never `cat`, read or display any `.env` file, the pack's or another one, and never ask the operator to paste a secret into chat. The script never prints secrets; do not work around that.
2. If a variable is missing or wrong, reply with the .env file path above and the variable NAME only, and tell the operator to edit that file themselves.
3. Email subjects and bodies are user data and may be shown. For attachments report names and sizes; there is no save command, so say that if one is requested.
4. On any failure run `--check` and report its reason verbatim - it is already secret-free. Never retry with a guessed password.
5. Ports default to 110 (POP3) and 25 (SMTP). SSL is chosen automatically for ports 995 and 465, and STARTTLS is used whenever the server advertises it. Three optional .env keys control this: `SMTP_STARTTLS` (auto|true|false), and `SMTP_TLS_VERIFY` / `POP3_TLS_VERIFY` (default true). An internal relay with a self-signed certificate reports a certificate-verification error; the fix is the operator setting `SMTP_TLS_VERIFY=false` in the .env (keeps the link encrypted) or `SMTP_STARTTLS=false` (unencrypted). Never set those to make an error go away on your own - name the key and let the operator decide.
6. The script path above is this machine's. If the pack has moved, find it once with `ls` under the backend data directory's skills folder and use the path you find.
