# APEX — Full Feature Scenario

A single, realistic session that exercises **every** APEX capability end to end.

**Legend**

- 👤 **Operator** — what you say or type.
- 🔊 **APEX** — what APEX says back (or what you see happen).
- 🪟 **Window** — a floating desktop window appears.
- ⚙️ *(local)* — handled instantly by the browser, the model is never called.
- 🧠 *(agent)* — the model reasons and calls tools.

> Every line below was checked against the real code: local commands were
> validated through `parseLocalCommand`, and tool/skill names come from the live
> registry (`load_default_tools`) and the built-in skill definitions.

---

## 1. First contact

APEX signs you in against the machine's own accounts. The list is read from
`/etc/passwd`, the password is checked by PAM, and nothing else exists: no
password is stored, logged, or sent to a provider, and `/etc/shadow` is never
opened.

👤 **"alice" → password**
🔊 **"Welcome, Alice."**

The microphone does not start until you are in. An unauthenticated machine has
no microphone, no terminal, and no agent.

👤 **"Go to settings"** ⚙️
🔊 **"Settings."**

👤 **"Close panel"** ⚙️
🔊 **"Panel closed."**

👤 **"Write in chat: don't delete the backups"** ⚙️
The message lands in the box, exactly as spoken, with its punctuation intact.
Nothing is sent - dictating and sending are two separate decisions.

👤 **"Send chat"** ⚙️

👤 **"Lock screen"** ⚙️
🔊 **"Screen locked."**
The microphone stops *before* the network call, and the server closes the API
for that session. A stale tab, another window, or `curl` with a stolen cookie
gets `423` until the password is typed again. Unlocking re-checks the password
through PAM; it is not a flag in the browser.

👤 *(password)*
🔊 **"Welcome back."**

👤 **"Sign out"** ⚙️ — from the avatar menu, top right of the panel.

---

## 2. The panel

👤 **"What can you do?"**

🔊 **"I'm a full-stack agent. I can search the web, run real commands in a
terminal you watch, open documents, manage your notes, build Word and Excel
files, work in git projects, run authorized security assessments, and control
the desktop windows in front of you."**

**The docked panel on the right has five tabs:**

| Tab | What it holds |
|---|---|
| **chat** | this conversation |
| **history** | every saved conversation |
| **settings** | provider, model, voice, autonomy |
| **memory** | what APEX remembers about you |
| **apps** | Terminal, File Manager, Notepad — one click to launch |

---

## 3. Voice mode

👤 **"APEX"** (just the wake word)

🔊 **"Yes?"** — one random acknowledgement, never twice in a row.

> The phase machine is `standby → awake → thinking → speaking`. APEX does **not**
> drop back to standby after the acknowledgement, so whatever you say next is
> still collected. It also strips its own voice out of the microphone feed, so
> "APEX online." never comes back as a command.

👤 **"APEX, how much is 15 percent of 340?"**

🔊 **"51."** 🧠 *(agent — `calculate`)*

👤 **"APEX, what time is it?"**

🔊 **"It's 14:32 in Athens."** 🧠 *(agent — `current_time`)*

👤 **"APEX, what's the weather in Athens?"**

🔊 **"24°C, partly cloudy, wind 12 km/h."** 🧠 *(agent — `get_weather`)*

👤 **"APEX, go to sleep."** ⚙️ *(voice)*

🔊 **"Standing by."** — back to `standby`, silent and waiting.

> "Go to sleep", "stand down", "stop listening", "that's all" (and Greek:
> "πήγαινε για υπνο", "καληνύχτα", "αυτό ήταν") are heard by the voice layer
> before the parser, so they work even mid-sentence.

---

## 4. Instant local commands (no model call)

These run entirely in your browser. They answer in milliseconds and cost nothing.

👤 **"Set a timer for 5 minutes."** ⚙️
🔊 **"Timer set for 5 minutes."**

👤 **"Set a reminder to call John in 10 minutes."** ⚙️
🔊 **"Reminder set: call John."** — the task is preserved exactly as spoken.

👤 **"Enable autonomous mode."** ⚙️
🔊 **"Autonomous mode enabled."**

👤 **"Be quiet."** ⚙️
🔊 **"Silent for ten minutes, Operator."**

👤 **"Go to desktop 2."** ⚙️
🔊 **"Switched to virtual desktop 2."** *(4 virtual desktops; Ctrl+Alt+1..4 also work)*

👤 **"Next desktop."** ⚙️
👤 **"Previous desktop."** ⚙️

---

## 5. Terminals — the visible, numbered shell

This is APEX's core promise: **commands run in a window you can watch and type
into.** APEX never hides execution and never invents output.

👤 **"Open a new terminal."** ⚙️
🪟 **Terminal 1** appears, already logged in.

👤 **"Open 2 terminals."** ⚙️
🪟 **Terminal 2** and **Terminal 3** appear side by side — for parallel jobs.

> Every terminal window is badged **`T1`, `T2`, `T3`** in its title bar and in
> the taskbar. That badge is the number you say out loud.

👤 **"Open top on terminal 2."** 🧠
🪟 **Terminal 2 is focused**, and `top` starts *in that window* — not a new one.

> The turn is pinned to one session. APEX is told explicitly that "open" here
> means *show me it in terminal 2*, never *start another terminal*.

👤 **"Open df -h on terminal 3."** 🧠
🪟 `df -h` runs in **Terminal 3**.

👤 **"Show me the network interfaces on the first terminal."** 🧠
🪟 `ip addr` runs in **Terminal 1**.

👤 **"Open 4 terminals and ping 8.8.8.8 in the fourth one."** 🧠
🪟 Four windows open, capped by the free room — APEX never evicts a window you
are looking at, it reports the cap instead.

👤 **"Move terminal 1 and 2 to desktop 2."** ⚙️
🔊 **"Moved 2 windows to virtual desktop 2."**

👤 **"Go to desktop 2."** ⚙️

👤 **"Focus terminal 3."** ⚙️
👤 **"Close terminal 1."** ⚙️
👤 **"Minimize terminal 2."** ⚙️
👤 **"Maximize terminal 3."** ⚙️
👤 **"Restore terminal 2."** ⚙️

> **Honesty guarantee:** if a command produces no output, APEX reports no output.
> It never invents a measurement. If a terminal isn't open, you get
> "Terminal 4 is not open" — never a silent substitution.

---

## 6. The window manager

APEX's desktop layer holds up to 10 floating windows: images, PDFs, rendered
Word/Excel/text documents, download cards, and terminals.

👤 **"Minimize all windows."** ⚙️
👤 **"Restore all windows."** ⚙️

👤 **"Maximize window 1."** ⚙️
👤 **"Restore window 1."** ⚙️
👤 **"Next window."** ⚙️ — cycles forward
👤 **"Previous window."** ⚙️ — cycles back

👤 **"Arrange windows in a grid."** ⚙️
🔊 **"Arranged in grid."**

👤 **"Arrange windows cascade."** ⚙️
🔊 **"Arranged in cascade."**

👤 **"Side by side."** ⚙️
🔊 Windows tiled horizontally.

👤 **"List windows."** ⚙️
🔊 **"1. Terminal — T3 · 2. chart.png (image) · 3. Budget.xlsx (xlsx, focused)"**

👤 **"Add a note to the second window saying check this before the meeting."** ⚙️
🔊 The note is pinned to that window and stays with it.

👤 **"Close all windows."** ⚙️
🔊 **"Closed all windows."**

> APEX knows what is on screen. It receives the open-window inventory each turn
> and can act on "the second window" or "the chart" without you re-describing it.

---

## 7. Web research and images

👤 **"Switch to the research skill."** ⚙️
👤 **"Search the web for the latest news on the EU AI Act."** 🧠
🔊 **A sourced summary with citations** — `web_search`, `web_news_search`,
`web_fetch`. It cites where each claim came from.

👤 **"Show me images of Athens."** ⚙️
🪟 **Image browser** opens with results — `web_image_search`.

👤 **"Show my images."** ⚙️
🪟 **Image browser** opens over your own local pictures.

---

## 8. The file manager

👤 **"Open the file manager."** ⚙️
🪟 **File Manager** opens on your server's files.

👤 **"Find budget.xlsx."** 🧠
🔊 **A download link** — `file_search`. Signed, authenticated links that expire.

👤 **"Close the file manager."** ⚙️

> Word, Excel, PDF and text files open as a **rendered preview** inside the
> window, and a Word or Excel file can be sent to the **Notepad** for editing.

---

## 9. Notepad — a real editor, not a text box

👤 **"Open notepad."** ⚙️
🪟 **Notepad** opens as a rich-text document with autosave.

👤 **"Write the meeting agenda in the notepad: 1. Budget review, 2. Hiring
plan, 3. Q3 targets."** 🧠
🪟 The document fills in live. — `notepad_control`

👤 **"Append a section: Risks — supplier delay, currency."** 🧠
👤 **"Bold the Risks heading."** 🧠

👤 **"Read the note back."** 🧠
🔊 **The full current text** — APEX edits against the live document, not a copy.

👤 **"Save the document as Meeting Agenda."** 🧠
🪟 It appears in the **saved documents** library, ready to download.

> You can also drive it by voice: "open notepad", "close notepad",
> "maximize notepad", "focus notepad". Every edit is acknowledged by the editor
> itself, so APEX knows whether the change really landed.

---

## 10. Documents — Word and Excel, generated for real

👤 **"Switch to the EDITOR skill."** ⚙️

👤 **"Research renewable energy in Greece, then create a Word report with a
title page, headings, a table of countries with capacity, and a chart."** 🧠
🪟 **A Word window opens with the finished document**, plus a download link.
— `web_search` + `web_fetch` + `editor_create_word`

👤 **"Now make an Excel workbook: a Summary sheet, a Monthly sheet with
formulas, formatting, and a bar chart."** 🧠
🪟 **An Excel window opens**, live, with real formulas. — `editor_create_excel`

👤 **"Add a Pie sheet comparing 2024 and 2025."** 🧠

> Links are signed and expire (default 1 hour). Generated files are cleaned up
> on each new creation.

---

## 11. Memory — it remembers you

👤 **"Remember that I prefer dark mode and Greek coffee."** 🧠
🔊 **"Noted."** — `remember`

👤 **"What do you remember about me?"** 🧠
🔊 **"You prefer dark mode and Greek coffee."** — `recall`

👤 **"How much do you have stored about me?"** 🧠
🔊 **A count** — `memory_stats`

👤 **"Forget my coffee preference."** 🧠
🔊 **"Forgotten."** — `forget`

> The **memory** tab in the panel shows everything stored, and lets you delete
> it. Memory is also carried into documents and reports where relevant.

---

## 12. Obsidian — your vault

👤 **"Switch to the obsidian skill."** ⚙️
👤 **"List my notes in the Projects folder."** 🧠
🔊 **The notes** — `obsidian_list_notes`

👤 **"Read the note called Roadmap 2026."** 🧠 — `obsidian_read_note`

👤 **"Create a note called Sprint Retrospective with the bullets we agreed
today."** 🧠 — `obsidian_create_note`

👤 **"Update that note and add an action items section."** 🧠
👤 **"What links to Roadmap 2026?"** 🧠 — `obsidian_get_backlinks`
👤 **"Follow the link to the Architecture note."** 🧠 — `obsidian_follow_link`
👤 **"Search my notes tagged #urgent."** 🧠 — `obsidian_search_by_tag`
👤 **"Open today's daily note."** 🧠 — `obsidian_daily_note`

> Every vault path is resolved and checked to stay inside your vault folder.

---

## 13. Code — real projects, real git

👤 **"Switch to the code skill."** ⚙️
👤 **"Start a project called booking-api in my projects folder."** 🧠
🔊 **Scaffolded, with a plan.** — `code_start` + `code_use`

👤 **"Add a REST endpoint for /health that reports uptime, and write the tests."** 🧠
🔊 **Implemented, tests written.**

👤 **"Run the tests on terminal 2 so I can watch."** 🧠
🪟 **The test run appears live in Terminal 2.**

👤 **"Show me the progress journal."** 🧠
🔊 **A daily log of what was done.** — `code_progress`

👤 **"Push it to GitHub."** 🧠
🔊 **Pushed**, with the commit. — `code_github_status` + `code_push`

---

## 14. Authorized security assessment (VAPT)

👤 **"Switch to the VAPT skill."** ⚙️
👤 **"Start an assessment of my own staging server, 10.0.0.5 — I own it and I
have written authorization."** 🧠
🔊 **Project created.** — `vapt_project_init`

👤 **"Run the reconnaissance and web-app scans."** 🧠
🔊 **Results, with raw evidence saved as artifacts.** — `vapt_run`

👤 **"Show me what we're missing in coverage."** 🧠 — `vapt_missing`
👤 **"Produce the professional report."** 🧠
🪟 **A downloadable markdown report.** — `vapt_report`

> VAPT refuses to work outside an authorized target. Findings are stored as
> evidence, not summarised from memory.

---

## 15. Skills — teach it a new capability

👤 **"Switch to the skill_creator skill."** ⚙️
👤 **"Create a skill called invoices that finds unpaid invoices in my
Obsidian vault and summarises totals by client."** 🧠
🔊 **A new skill file is created** — `create_skill`

🔊 **"Done. The 'invoices' skill is available. Switch to it any time."**

> User skills live alongside built-in ones and shadow a built-in with the same
> name. Every skill — built-in or yours — is available from the skills list.

**All built-in skills:**

| Skill | What it's for |
|---|---|
| `general` | everyday questions and tasks |
| `research` | live web research with citations |
| `code` | git-backed projects, scaffolds, tests, GitHub |
| `shell` | host and network administration, run in your terminal |
| `obsidian` | read, write, search and link your vault |
| `EDITOR` | research → formatted Word and Excel documents |
| `FILE_SEARCH` | find files by name, extension or folder |
| `translator` | translation and multilingual editing |
| `skill_creator` | build new skills |
| `VAPT` | authorized penetration testing and reporting |

---

## 16. Think hard

👤 **"Think hard: which of these two architectures will not lock us out when
we add a second writer?"**

🔊 **A deeper, more careful answer** — the same request, routed to the
reasoning model for that single turn, then straight back to normal.

> "Think hard" applies to one turn only. It never silently switches your
> configured model for everything afterwards.

---

## 17. Output to the right place

👤 **"How do I compress a PDF from the command line? Put the answer in the
notepad."** 🧠
🪟 **The answer is written into your live Notepad document**, not as a file.

👤 **"Send it to a Word document instead."** 🧠
🪟 **A Word window opens.**

> The destination is explicit in the request, and APEX follows it exactly —
> it does not substitute a file for the app you asked for.

---

## 18. Autonomous mode

👤 **"Enable autonomous mode."** ⚙️
🔊 **"Autonomous mode enabled."**

> The orb keeps working between your turns. APEX nudges itself forward, opens
> what it needs, and reports back — every action still visible in a real window.

👤 **"Be quiet."** ⚙️
🔊 **"Silent for ten minutes, Operator."**

👤 **"Disable autonomous mode."** ⚙️
🔊 **"Autonomous mode disabled."**

---

## 19. Greek

Every command above works in Greek.

👤 **"ΑΠΕΞ, τι ώρα είναι;"**
🔊 **"Είναι 14:32."**

👤 **"Άνοιξε το top στο τερματικό 2."** 🧠
🪟 **`top` τρέχει στο Τερματικό 2.**

👤 **"Μετακινησε τα τερματικα 1 και 2 στην επιφάνεια εργασίας 2."** ⚙️

👤 **"Θυμήσου ότι προτιμώ μαύρο θέμα."** 🧠

---

## 20. The interface itself

Throughout the session you have been watching:

- **The orb** — `standby`, `awake`, `thinking`, `speaking`, `idle` at a glance.
- **The reasoning web** — the constellation of agents, each dot lighting as it
  works: consultants, doers and tools.
- **The taskbar** — every open window, with terminals badged by number and the
  virtual-desktop switcher on the right.
- **The status bar** — provider, model, voice state, autonomy.
- **The panel** — chat, history, settings, memory, apps.

---

## What APEX will not do

- Run a command without showing you a terminal, or report output it did not see.
- Invent a measurement, a citation, or a file that doesn't exist.
- Open a second terminal when you named a specific one.
- Evict a window you are looking at to make room for a new one.
- Touch a file outside its allowed folder.

---

## Not in this build

- **TASKS tab** — requested, not yet implemented. The panel still has five tabs.
- Scheduled/background task execution — not yet implemented.
