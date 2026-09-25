# APEX User Manual

A practical guide to using APEX through chat, voice, and its desktop apps.

## Contents

- [Getting started](#getting-started)
- [Voice and typed commands](#voice-and-typed-commands)
- [Quick command reference](#quick-command-reference)
- [Notepad](#notepad)
- [Terminal](#terminal)
- [Files, images, and document previews](#files-images-and-document-previews)
- [Windows and virtual desktops](#windows-and-virtual-desktops)
- [Timers and reminders](#timers-and-reminders)
- [Skills and everyday requests](#skills-and-everyday-requests)
- [Memory and conversations](#memory-and-conversations)
- [Settings and autonomous mode](#settings-and-autonomous-mode)
- [Greek command examples](#greek-command-examples)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [What is saved](#what-is-saved)
- [Troubleshooting](#troubleshooting)

## Getting started

1. Open the APEX address supplied by your administrator. A local installation
   normally serves the interface on port 3000.
2. Sign in if prompted. Some personal installations sign you in automatically.
3. Open **CHAT**, type a message, and press **Enter**. Use **Shift+Enter** for a
   new line.
4. Choose **APPS** to launch **Notepad**, **Terminal**, or **File Manager**.
5. For speech, enable **MIC ON**, allow microphone access, and say
   **“Apex, open notepad.”**

The other main tabs are **HIST** for conversations, **SETTINGS** for preferences,
and **MEMORY** for stored information. The skill buttons select the assistant's
area of work. Collapse the assistant panel when you want more desktop space.

For installation and account setup, see [README.md](README.md). For service
administration, see [Linux services](docs/linux-services.md).

## Voice and typed commands

### Speaking to APEX

The default wake word is **Apex**. You can say the wake word and request together,
or say the wake word, wait for the listening cue, and then give the request.
After a reply, the configured follow-up window lets you continue without saying
“Apex” again.

- **“Apex, open notepad.”** — wake and give a command.
- **“Write to notepad Meeting at ten.”** — a follow-up while listening.
- **“Sleep”**, **“stop listening”**, **“goodbye”**, or **“that's all”** — finish the
  spoken exchange and return to wake-word standby.
- Say the wake word while APEX is speaking to interrupt its speech and give a
  new request. This does not undo an action already performed.
- Switch **MIC OFF** to turn voice input off completely. Sleep leaves wake-word
  listening enabled.

Use a browser supported by APEX's speech-recognition interface; its error
messages recommend Chrome or Edge when recognition is unavailable. Microphone
access needs localhost or a secure HTTPS page. If voice is unavailable, use the
same action wording in chat.

### Command wording

The command tables below contain **direct commands** recognized by the app.
Type them without a wake word; for voice, say the wake word first when APEX is
in standby. English commands work regardless of the selected response language.
Greek shortcuts are available when the response language is Greek.

More flexible requests, generated writing, research, and multi-step instructions
use the AI model and its enabled tools. These need a working provider/model and
may take longer. Available tools depend on the installation. The model can ask
for missing details or report that a feature is disabled.

Use exact filenames and paths in typed chat when speech recognition could
mishear them. Include an app name, such as “close notepad,” to make the target
clear. Do not assume every paraphrase is a direct shortcut.

## Quick command reference

| Command | Result |
|---|---|
| `open notepad` | Open or focus Notepad. |
| `open new notepad` | Open a separate, empty Notepad window. |
| `write to notepad Remember to call Maria.` | Append the supplied text. |
| `save notepad` | Save the current Notepad document. |
| `open terminal` | Open or focus a terminal. |
| `open new terminal` | Create another terminal session. |
| `open file manager` | Open or focus File Manager. |
| `list windows` | List open windows and their numbers. |
| `arrange windows in a grid` | Arrange windows on the current desktop. |
| `go to desktop 2` | Switch to desktop 2. |
| `set a timer for 5 minutes` | Start a browser timer. |
| `remind me in 10 minutes to call Maria` | Schedule a browser reminder. |
| `show images of mountains` | Search for web images. |
| `use research` | Select the research skill. |
| `stop talking` | Stop speech and temporarily silence autonomous chatter. |

## Notepad

### Opening and editing

Open Notepad from **APPS** or use “open notepad.” Launching it from APPS creates
a fresh window; the direct open command focuses an existing Notepad when one
is available. Say or type **“open new notepad”** (also **“open another notepad”**)
to create a separate empty window and keep your existing notes open. **“New
notepad”** without “open” starts a new document in the existing editor.

The left document panel **starts closed**. Click **RECENT DOCUMENTS** to load and
open it. Search by filename, select a saved document, or choose **NEW DOCUMENT**.
Use the panel's hide button to close the panel without closing your document.

Change the title at the top. The toolbar supports paragraphs, headings,
blockquote, bold, italic, underline, strikethrough, lists, alignment, links,
undo/redo, and removing formatting. Select text before using a toolbar button
to format that selection. Pasted content is inserted as plain text.

The bottom bar shows word/character counts, unsaved changes, and document
status. **0 words · 0 characters is normal for an empty document.**

### Saving, importing, and downloading

- Click **SAVE** or press **Ctrl+S / Cmd+S** with the editor active. A new document
  needs its first save; after that, edits autosave after a short pause.
- **HTML ↓** saves the current document and downloads a standalone HTML file.
- **TXT ↓** downloads the current text without rich-text formatting. It does not
  itself save a copy in the document library.
- **IMPORT TXT** imports a text file. Markdown files are imported as plain text,
  not rendered Markdown. The file-size limit is 2 MB.
- Starting a new document, opening another one, or closing a window with unsaved
  changes can prompt before discarding them. A save in progress can temporarily
  prevent closing.

Saved documents belong to your signed-in APEX user. By default they live in
`~/Documents/APEX Notepad/<user>/` on the **backend machine**, under the account
running APEX. Administrators can change this with `NOTEPAD_DOCUMENTS_DIR`.
New documents with the same title receive numbered filenames. Changing an
existing document's title does not change its saved filename.

### Direct Notepad commands

These target the focused Notepad, otherwise the latest Notepad on the current
desktop, falling back to another open Notepad. Editing commands open Notepad if
necessary. Focus the intended window first when several are open.

| Command | Result |
|---|---|
| `focus notepad` | Bring Notepad forward. |
| `minimize notepad` | Minimize its window. |
| `maximize notepad` | Maximize its window. |
| `restore notepad` | Return it to its normal window state. |
| `close notepad` | Close it, subject to unsaved-change protection. |
| `new notepad` | Start a new document. |
| `open notepad and write Meeting notes.` | Open it and append literal text. |
| `write Call Maria at five in notepad` | Append literal text to the end. |
| `in the notepad, write Agenda for Monday` | Another writing form. |
| `append More notes in notepad` | Append more text. |
| `replace notepad contents with Updated meeting notes.` | Replace the whole document with plain text. |
| `clear notepad` | Clear the document; use undo if needed. |
| `rename notepad to Meeting Notes` | Change the document title. |
| `show recent documents in notepad` | Open the document panel. |
| `hide recent documents` | Hide the document panel. |
| `open Meeting Notes in notepad` | Open a saved filename, with or without `.html`. |
| `read notepad` | Return the current document text. |
| `select all in notepad` | Select its text. |
| `undo in notepad` | Undo an editor operation. |
| `redo in notepad` | Redo an editor operation. |
| `format notepad bold` | Apply/toggle bold across the whole document. |
| `format notepad italic` | Apply/toggle italic across the whole document. |
| `format notepad underline` | Apply/toggle underline across the whole document. |
| `format notepad heading 1` | Apply a heading format. |
| `format notepad heading 2` | Apply a second-level heading format. |
| `format notepad paragraph` | Apply paragraph format. |
| `format notepad bullet list` | Apply/toggle a bulleted list. |
| `format notepad numbered list` | Apply/toggle a numbered list. |
| `format notepad align left` | Align the document left. |
| `format notepad align center` | Center its text. |
| `format notepad align right` | Align the document right. |
| `download notepad` | Save and download HTML. |
| `export notepad as text` | Download plain text. |
| `open notepad and add command output` | Append the latest available command-tool output from this conversation. |
| `copy the last terminal output to notepad` | Copy available command-tool output without rerunning the command. |

Voice/text formatting commands act on the whole document; use the toolbar for
selection-only formatting. Clear and replace modify the document immediately,
so save a copy first if you want to retain the original.

### Flexible and multi-step requests

These are **AI-assisted requests**, not fixed shortcuts:

- “Open notepad and write a poem about spring, then save it.”
- “Summarize this conversation in Notepad and title it Project Summary.”
- “Rewrite the current Notepad text in a more formal tone.”
- “Run uname -a in the terminal and put its output in Notepad.”
- “Show me the manual of the command df and show it on Notepad.”
- “Open Meeting Notes in Notepad, add the action items below, then save.”

For terminal execution, open a terminal first. APEX should execute a requested
command once and copy its actual result. Asking to copy existing output does
not rerun it. The direct output-copy shortcut uses command-tool results
available in the current conversation; it does not scrape arbitrary commands
typed manually into a terminal or recover output from a previous browser session.

AI-assisted rewriting receives the current unsaved text, capped at 16,000
characters per document. For longer notes, request changes in smaller sections.
Explicit Notepad destinations keep manuals and captured shell output in the live
editor, instead of auto-opening a separate text preview. If a command manual is
unavailable on the host, APEX should report that retrieval failure.
Watch the editor and its action acknowledgements for completion or cancellation.

## Terminal

### Window controls

| Command | Result |
|---|---|
| `open terminal` | Open or focus a terminal. |
| `open another terminal` | Create another terminal. |
| `focus terminal 2` | Focus the second terminal. |
| `minimize terminal` | Minimize a terminal window. |
| `maximize terminal` | Maximize a terminal window. |
| `restore terminal` | Restore its normal size. |
| `close terminal` | Close a terminal window. |

Terminals operate on the **machine running the backend**. Shell access must be
enabled by the administrator. You can type directly into the terminal or ask
APEX to work in it.

### Writing versus running commands

The following requests use the model and terminal tools:

| Request | Intended behavior |
|---|---|
| “Write pwd in the terminal.” | Type the command without pressing Enter. |
| “Run pwd in the terminal.” | Type it and press Enter to execute. |
| “Execute the command you just typed.” | Submit the prepared command. |
| “Run ls -lah in terminal two.” | Execute in the named terminal. |

Commands default to the focused terminal when available. Be explicit about
**write/type** versus **run/execute**. For a sudo password prompt, enter your
password directly into the terminal; do not put it in chat. Restarting the
backend ends active terminal sessions.

## Files, images, and document previews

### File Manager

| Command | Result |
|---|---|
| `open file manager` | Open or focus File Manager. |
| `open new file manager` | Open another File Manager window. |
| `focus file manager` | Bring it forward. |
| `minimize file manager` | Minimize it. |
| `maximize file manager` | Maximize it. |
| `restore file manager` | Restore its normal size. |
| `close file manager` | Close its window. |

File Manager browses backend-host files. Use its folder navigation, list/grid
views, hidden-file toggle, upload controls, and file menus. Uploads can prompt
on name conflicts. Delete dialogs distinguish moving to Trash from permanent
deletion. Available roots and permissions depend on the installation.

To search with the assistant, ask, for example:

- “Find PDF invoices in my Documents folder.”
- “Find files matching `*.xlsx` in `/home/me/Documents`.”

File search matches filenames, extensions, and locations; it is not a search
inside document contents. For content retrieval, use memory document upload or
an appropriate document-reading tool. Paths refer to the backend machine, not
necessarily the computer displaying your browser.

### Images and previews

| Command | Result |
|---|---|
| `show images of mountains` | Search web images. |
| `show local images` | Browse configured local images. |
| `show my pictures` | Open local picture browsing. |
| `show images of cats from my computer` | Search local images for cats. |
| `next image` | Advance within the focused gallery. |
| `previous image` | Go back within the focused gallery. |

Click supported file/image links or preview controls to open floating windows.
Images and PDFs can display inline; supported Office and text documents can
have rendered previews. Unsupported types may offer a download card instead.
A gallery can contain several items in one window.

Generated Word/Excel documents and file-search results use download links that
can expire; the usual default is one hour. If a link expires or the source file
changes, repeat the search or generation to obtain a new link.

## Windows and virtual desktops

Drag a title bar to move a window and its bottom-right handle to resize it.
Use its title-bar controls and the taskbar to focus, minimize, restore, or close
it. APEX supports up to **10 windows** and **4 virtual desktops**.

| Command | Result |
|---|---|
| `list windows` | Show window numbers for targeting. |
| `focus window 2` | Focus the numbered window. |
| `maximize window 2` | Maximize it. |
| `minimize window 2` | Minimize it. |
| `restore window 2` | Restore it. |
| `close window 2` | Close it. |
| `close all windows` | Request closing all windows; Notepad may prevent discarding edits. |
| `minimize all windows` | Minimize windows. |
| `restore all windows` | Restore minimized windows. |
| `arrange windows in a grid` | Grid layout on the current desktop. |
| `cascade the windows` | Overlapping cascade layout. |
| `side by side` | Arrange in columns. |
| `stack` | Arrange in rows. |
| `center` | Center windows. |
| `add a note to the second window saying Review this tomorrow` | Attach a window note. |
| `go to desktop 1` | Switch to desktop 1. |
| `switch to desktop 4` | Switch to desktop 4. |
| `next desktop` | Move to the next desktop. |
| `previous desktop` | Move to the previous desktop. |
| `move window 2 to desktop 3` | Move a numbered window to another desktop. |

Use “list windows” before referring to a number. Window notes are temporary
annotations, separate from saved Notepad documents. “Next image” advances a
gallery item; it is not a general next-app shortcut.

## Timers and reminders

| Command | Result |
|---|---|
| `set a timer for 5 minutes` | Start a five-minute timer. |
| `start a timer for 1 hour and 30 minutes` | Start a combined-duration timer. |
| `set a timer for 10 minutes called Tea` | Start a named timer. |
| `remind me in 20 minutes to check the oven` | Schedule a relative reminder. |
| `remind me to call Maria at 15:30` | Schedule the next occurrence of 15:30. |
| `remind me at 3 PM to join the meeting` | Use a 12-hour clock time. |
| `cancel all timers` | Cancel timers. |
| `cancel all reminders` | Cancel reminders. |

Digits and common number words work. Absolute clock times use the browser's
local time; if the time has already passed today, the reminder is for tomorrow.
Use `15:30` or `3 PM`, rather than an ambiguous bare `3`.

Timers and reminders run in the current browser session. Keep APEX open; a
reload clears them, and a sleeping device or suspended tab can delay alerts.
They are not a background calendar service. Cancellation shortcuts cancel the
category, not an individual reminder by name.

## Skills and everyday requests

Choose a skill button or switch using its installed name:

| Command | Result |
|---|---|
| `use general` | Return to general assistance. |
| `switch to research` | Select research. |
| `use translator` | Select translation. |
| `use EDITOR` | Select Word/Excel document generation. |
| `use FILE_SEARCH` | Select file search. |
| `use code` | Select coding assistance. |
| `use obsidian` | Select the configured Obsidian vault assistant. |
| `use shell` | Select host-system assistance. |
| `use VAPT` | Select the installed security-assessment skill. |
| `use skill_creator` | Select skill creation. |

A switch can include a request: **“Use research, compare these three products.”**
Custom skills work with their exact installed names. In general mode, automatic
routing can select a specialist for a turn without permanently changing your
chosen skill.

Examples of **AI-assisted requests**:

| Task | Example |
|---|---|
| Weather/time | “What is the weather in Athens?” / “What time is it?” |
| Research | “Research this topic and include links to your sources.” |
| Translation | “Translate this email into Greek, keeping a professional tone.” |
| Word document | “Create a Word report with a summary, table, and recommendations.” |
| Excel workbook | “Create an Excel budget with monthly totals and a chart.” |
| Coding | “Review this project, fix the failing test, and explain the change.” |
| Obsidian | “Find my meeting notes and add these action items to today's note.” |
| New skill | “Create a skill that helps me draft project risk assessments.” |
| Security assessment | “Assess my authorized test server at this address and produce a report.” |

Word/Excel generation is the **EDITOR** skill; live rich-text editing is the
**Notepad app**. Obsidian is a separately configured vault. Some tools require
administrator configuration, installed dependencies, credentials, or a specified
project/target before they can work.

## Memory and conversations

Use **HIST** to reopen or delete conversations. Start a new conversation for a
separate task. Chat history and saved Notepad documents are separate.

The **MEMORY** tab lets you add, search, and delete stored information and upload
supported documents for retrieval. Examples for the assistant:

- “Remember that I prefer concise answers.”
- “What do you remember about my current project?”
- “Use the document I uploaded to answer this question.”

Memory retrieval depends on the installation's memory configuration. Deleting
a chat is not a substitute for reviewing and deleting stored memory. Saving a
Notepad document is not the same action as uploading a document into memory.

## Settings and autonomous mode

| Setting | What it changes |
|---|---|
| Engine, provider, model | Which configured backend handles AI requests. |
| Temperature | Response variability for models that support it. |
| Wake word | The word that starts a spoken exchange. |
| Default response language | Reply language and language-specific command recognition. |
| Follow-up window | How long you can continue speaking without another wake word. |
| TTS voice name | Preferred installed browser speech voice. |
| Spoken replies | Whether replies are spoken aloud. |
| Autonomous mode | Whether APEX initiates periodic activity when appropriate. |
| Humor and sarcasm | Tone preferences. |
| Daily voice budget | Controls autonomous speech activity. |

Changing a model/provider does not install or configure it. Choose an available
option, or ask the administrator to configure the desired provider.

| Command | Result |
|---|---|
| `enable autonomous mode` | Enable autonomous activity. |
| `disable autonomous mode` | Disable autonomous activity. |
| `stop talking` | Stop speech and temporarily silence autonomous chatter. |
| `I am your operator Maria` | Set the operator identity used by the interface. |

Voice sleep, MIC OFF, spoken-reply settings, and autonomous mode are separate
controls. To stop autonomous activity until you turn it back on, use “disable
autonomous mode.”

## Greek command examples

Select **Greek** in **Default response language**. English commands remain
available. These examples use the built-in Greek command forms:

| Command | Meaning |
|---|---|
| `άνοιξε σημειωματάριο` | Open Notepad. |
| `γράψε Καλημέρα στο σημειωματάριο` | Append “Καλημέρα” to Notepad. |
| `αποθήκευσε σημειωματάριο` | Save Notepad. |
| `κλείσε σημειωματάριο` | Close Notepad. |
| `άνοιξε τερματικό` | Open a terminal. |
| `κλείσε όλα τα παράθυρα` | Close all windows. |
| `βάλε χρονόμετρο για πέντε λεπτά` | Start a five-minute timer. |
| `θύμισέ μου σε δέκα λεπτά να καλέσω τη Μαρία` | Schedule a reminder. |
| `ακύρωσε όλα τα χρονόμετρα` | Cancel timers. |
| `ακύρωσε όλες τις υπενθυμίσεις` | Cancel reminders. |
| `δείξε εικόνες με βουνά` | Search images of mountains. |
| `χρησιμοποίησε έρευνα` | Select research. |
| `απενεργοποίησε αυτόνομη λειτουργία` | Disable autonomous mode. |

During a spoken exchange, **“κοιμήσου”**, **“καληνύχτα”**, or **“αντίο”** returns
to standby. For a Greek command with the default English wake word, say “Apex,”
wait for the listening cue, then speak Greek. Flexible Greek requests can also
be handled by the model when they are not direct shortcuts.

## Keyboard shortcuts

| Keys | Where / effect |
|---|---|
| **Enter** | Send from the chat composer. |
| **Shift+Enter** | Insert a newline in chat. |
| **Ctrl+S / Cmd+S** | Save while working in Notepad. |
| **Ctrl+Alt+1 … 4** | Switch virtual desktops when the desktop handler receives the keys. |
| **Ctrl+Alt+Left / Right** | Previous/next virtual desktop. |
| **Escape** | Close a focused preview window; Terminal, File Manager, and Notepad own their keys. |
| **Left / Right** | Previous/next item in a focused preview gallery. |

An active editor, terminal, or browser/OS shortcut may consume a key combination.
Use app controls or voice/text commands when a shortcut does not reach APEX.

## What is saved

| Item | Lifetime / location |
|---|---|
| Saved Notepad documents | Backend filesystem, separated by signed-in user. |
| Downloaded HTML/TXT/Office files | Your browser's download location. |
| Conversations and settings | Backend account data. |
| Stored memory | Backend memory storage, when enabled. |
| Open windows, desktop arrangement, and window notes | Current frontend session. |
| Unsaved Notepad edits | Current editor session; save before leaving. |
| Timers and reminders | Current browser session. |
| Terminal sessions | Backend process; backend restarts end them. |

Ask the administrator to back up both APEX's data directory and its configured
Notepad document directory. Download important generated documents before their
links expire.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Notepad shows 0 words and 0 characters | This is normal until you type or open a document. |
| Recent documents reports 404 | The backend/proxy may be running an older version. Ask the administrator to restart updated services, reload the page, then use **Recent documents → Retry**. |
| Notepad is busy | Let its save/open operation finish, then retry. |
| My new note is missing | New documents need an initial save. Check the signed-in account and Recent documents search. |
| Renamed note still has the old filename | Title changes do not rename an existing saved file. Search its original filename. |
| No command output is available | Run a command through APEX in this conversation first, or name the command you want it to run and copy. |
| Microphone permission is pending/denied | Allow microphone access for the APEX site and check the browser's selected microphone. |
| Voice recognition is unavailable | Check the browser message, use a supported browser over HTTPS/localhost, or type the command. |
| APEX mishears a path or command | Type exact paths, filenames, and shell syntax in chat. |
| Voice stays on processing after opening a terminal | Update and reload the frontend. Completed app commands should return to listening even without a spoken reply; opening a terminal now has an acknowledgment. |
| APEX listens but does not speak | Check Spoken replies, the selected voice, and system/browser volume. |
| The wrong app/window changes | Focus the intended window or use a numbered window/terminal command. |
| I cannot open another window | The limit is 10; close an unused window. |
| Terminal says it is disabled | Ask the administrator to configure shell access. |
| Download link expired | Repeat the search or document-generation request. |
| A reminder did not fire on time | Keep the page active; sleeping devices and suspended tabs can delay browser alerts. |
| A model/provider fails | Select an available configured model and ask the administrator to check credentials or its service. |
| Changes do not appear after an update | Save your work, reload the browser, and have the administrator restart the updated frontend/backend services. |

For setup details and more technical troubleshooting, use the
[README](README.md) and [service guide](docs/linux-services.md).
