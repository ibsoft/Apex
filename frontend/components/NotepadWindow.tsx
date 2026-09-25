"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { BASE, api } from "../lib/api";
import type { NotepadCommand } from "../lib/notepad";
import styles from "./NotepadWindow.module.css";

type Doc = { name: string; modified_at: number; size_bytes: number };

export default function NotepadWindow({ focused, windowId }: { focused: boolean; windowId: string }) {
  const revision = useRef(0);
  const busy = useRef(false);
  const importRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [text, setText] = useState("");
  const editorRef = useRef<HTMLDivElement>(null);
  const [documents, setDocuments] = useState<Doc[]>([]);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryError, setLibraryError] = useState("");
  const [name, setName] = useState<string>();
  const [title, setTitle] = useState("Untitled");
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [loadingDocument, setLoadingDocument] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("Ready · saves to Documents/APEX Notepad");
  const changed = () => {
    revision.current += 1;
    setContent(editorRef.current?.innerHTML ?? "");
    setText(editorRef.current?.innerText ?? "");
    setDirty(true);
  };
  const canReplace = () => !busy.current && (!dirty || window.confirm("Discard unsaved changes to this document?"));
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    const closing = (event: Event) => {
      const target = (event as CustomEvent<{ id?: string }>).detail?.id;
      if (target && target !== windowId) return;
      if (busy.current || (dirty && !window.confirm(`Discard unsaved changes to “${title}”?`))) event.preventDefault();
    };
    window.addEventListener("apex:window-before-close", closing);
    return () => window.removeEventListener("apex:window-before-close", closing);
  }, [dirty, title, windowId]);

  const refresh = useCallback(async () => {
    setLibraryLoading(true);
    setLibraryError("");
    try { setDocuments((await api.notepad.list()).documents); }
    catch (error: any) { setLibraryError(error?.status === 404 ? "Notepad storage is unavailable. Restart the APEX backend after updating, then retry." : error?.message || "Could not load documents"); }
    finally { setLibraryLoading(false); }
  }, []);


  const save = useCallback(async () => {
    if (busy.current) return undefined;
    busy.current = true;
    const savedRevision = revision.current;
    const html = editorRef.current?.innerHTML ?? content;
    setSaving(true);
    try {
      const result = await api.notepad.save({ name, title, content: html });
      setName(result.name);
      if (revision.current === savedRevision) {
        setTitle(result.title);
        setDirty(false);
      }
      setStatus(`Saved · ${result.directory}/${result.name}`);
      await refresh();
      return result.download_url;
    } catch (error: any) {
      setStatus(error?.message || "Save failed");
      return undefined;
    } finally { busy.current = false; setSaving(false); setLoadingDocument(false); }
  }, [content, name, refresh, title]);

  useEffect(() => {
    if (!name || !dirty || saving) return;
    const timer = setTimeout(() => void save(), 1200);
    return () => clearTimeout(timer);
  }, [content, dirty, name, save, saving, title]);

  const download = async () => {
    const url = await save();
    if (!url) return false;
    const anchor = document.createElement("a");
    anchor.href = BASE + url;
    anchor.download = name || "Untitled.html";
    anchor.click();
    return true;
  };
  const exportText = () => {
    const url = URL.createObjectURL(new Blob([editorRef.current?.innerText ?? ""], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `${title || "Untitled"}.txt`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const importText = async (file?: File) => {
    if (!file || !canReplace()) return;
    if (file.size > 2 * 1024 * 1024) { setStatus("Text files must be under 2 MB"); return; }
    busy.current = true;
    setLoadingDocument(true);
    setSaving(true);
    try {
      const value = await file.text();
      if (editorRef.current) editorRef.current.innerText = value;
      setName(undefined); setTitle(file.name.replace(/\.[^.]+$/, "")); changed();
      setStatus("Imported text · save to keep this document");
    } catch { setStatus("Could not read this file"); }
    finally { busy.current = false; setSaving(false); setLoadingDocument(false); }
  };

  const open = async (docName: string) => {
    if (!canReplace()) return false;
    busy.current = true;
    setLoadingDocument(true);
    setSaving(true);
    try {
      const doc = await api.notepad.get(docName);
      setName(doc.name); setTitle(doc.title); setContent(doc.content);
      revision.current += 1;
      setDirty(false); setStatus(`Opened · ${doc.name}`);
      if (editorRef.current) editorRef.current.innerHTML = doc.content;
      setText(editorRef.current?.innerText ?? "");
      return true;
    } catch (error: any) { setStatus(error?.message || "Could not open document"); return false; }
    finally { busy.current = false; setSaving(false); setLoadingDocument(false); }
  };
  const fresh = () => {
    if (!canReplace()) return false;
    revision.current += 1;
    setName(undefined); setTitle("Untitled"); setContent(""); setText(""); setDirty(false);
    if (editorRef.current) editorRef.current.innerHTML = "";
    setStatus("New document · save location: Documents/APEX Notepad");
    return true;
  };
  useEffect(() => {
    const context = (event: Event) => {
      (event as CustomEvent).detail.collect({ windowId, title, name, focused, dirty, text: (editorRef.current?.innerText ?? "").slice(0, 16000) });
    };
    window.addEventListener("apex:notepad-context", context);
    return () => window.removeEventListener("apex:notepad-context", context);
  }, [windowId, title, name, focused, dirty]);

  useEffect(() => {
    const control = async (event: Event) => {
      const detail = (event as CustomEvent<NotepadCommand & { windowId?: string; respond?: (message: string) => void }>).detail;
      if (!detail || (detail.windowId ? detail.windowId !== windowId : !focused)) return;
      event.preventDefault();
      const respond = (message: string) => { setStatus(message); detail.respond?.(message); };
      if (busy.current) { respond("Notepad is busy saving or opening a document. Try again when it finishes."); return; }
      try {
        switch (detail.action) {
          case "new": respond(fresh() ? "Created a new Notepad document." : "New document canceled; your changes are still open."); break;
          case "save": respond(await save() ? "Saved the Notepad document." : "Notepad save failed. Check the editor and try again."); break;
          case "download": respond(await download() ? "Downloaded the Notepad document." : "Notepad download failed because the document could not be saved."); break;
          case "export_text": exportText(); respond("Downloaded Notepad as text."); break;
          case "recent": setLibraryOpen(true); await refresh(); respond("Opened recent documents."); break;
          case "hide_recent": setLibraryOpen(false); respond("Hidden recent documents."); break;
          case "open_document": {
            const result = await api.notepad.list();
            const wanted = (detail.content ?? "").replace(/^['"]|['"]$/g, "").replace(/\.html$/i, "").toLocaleLowerCase();
            const doc = result.documents.find((item) => item.name.replace(/\.html$/i, "").toLocaleLowerCase() === wanted);
            respond(!doc ? "Document not found. Open recent documents to see saved filenames." : await open(doc.name) ? `Opened ${doc.name}.` : "Document was not opened; current changes were kept.");
            break;
          }
          case "read": respond(editorRef.current?.innerText || "Notepad is empty."); break;
          case "title": revision.current += 1; setTitle(detail.content || "Untitled"); setDirty(true); respond("Updated the Notepad title."); break;
          case "write":
          case "replace":
          case "clear": {
            const editor = editorRef.current;
            if (!editor) { respond("Notepad editor is not ready."); break; }
            editor.focus();
            const range = document.createRange(); range.selectNodeContents(editor);
            if (detail.action === "write") range.collapse(false);
            const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
            const value = detail.action === "clear" ? "" : detail.content ?? "";
            document.execCommand(value ? "insertText" : "delete", false, value);
            changed(); respond(detail.action === "write" ? "Added text to Notepad." : "Updated Notepad contents."); break;
          }
          case "undo": case "redo": command(detail.action); respond(`Notepad ${detail.action} applied.`); break;
          case "select_all": editorRef.current?.focus(); document.execCommand("selectAll"); respond("Selected all Notepad text."); break;
          case "format": {
            const formats: Record<string, [string, string?]> = {
              bold: ["bold"], italic: ["italic"], underline: ["underline"], strikethrough: ["strikeThrough"],
              "heading 1": ["formatBlock", "h1"], "heading 2": ["formatBlock", "h2"], paragraph: ["formatBlock", "p"],
              "bullet list": ["insertUnorderedList"], "numbered list": ["insertOrderedList"],
              "align left": ["justifyLeft"], "align center": ["justifyCenter"], "align right": ["justifyRight"],
              "remove formatting": ["removeFormat"],
            };
            const format = formats[detail.content ?? ""];
            if (!format) { respond("Unknown Notepad format."); break; }
            editorRef.current?.focus(); document.execCommand("selectAll"); command(...format);
            respond("Applied Notepad formatting."); break;
          }
          default: respond("Unsupported Notepad command.");
        }
      } catch (error: any) { respond(error?.message || "Notepad command failed."); }
    };
    window.addEventListener("apex:notepad", control);
    return () => window.removeEventListener("apex:notepad", control);
  }, [dirty, focused, loadingDocument, name, save, title, windowId]);

  const command = (cmd: string, value?: string) => {
    if (loadingDocument) return;
    editorRef.current?.focus();
    document.execCommand("styleWithCSS", false, "true");
    document.execCommand(cmd, false, value);
    changed();
  };
  const link = () => { const url = window.prompt("Link URL"); if (url && /^(https?:\/\/|mailto:|#)/i.test(url.trim())) command("createLink", url.trim()); else if (url) setStatus("Use an https://, http://, mailto: or # link"); };
  const tools: Array<[string, string, string?]> = [
    ["S", "strikeThrough"], ["P", "formatBlock", "p"], ["❝", "formatBlock", "blockquote"], ["B", "bold"], ["I", "italic"], ["U", "underline"], ["H1", "formatBlock", "h1"], ["H2", "formatBlock", "h2"],
    ["•", "insertUnorderedList"], ["1.", "insertOrderedList"], ["←", "justifyLeft"], ["↔", "justifyCenter"], ["→", "justifyRight"],
    ["↶", "undo"], ["↷", "redo"], ["Tx", "removeFormat"],
  ];

  return <div className={styles.shell} onKeyDown={(e) => {
    e.stopPropagation();
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); void save(); }
  }}>
    {libraryOpen && <aside className={styles.library} aria-label="Documents">
      <div className={styles.libraryHead}>
        <span>DOCUMENTS</span>
        <button className={styles.hideLibrary} onClick={() => setLibraryOpen(false)} title="Hide Documents" aria-label="Hide Documents">‹</button>
      </div>
      <button className={styles.newButton} onClick={fresh}>＋ NEW DOCUMENT</button>
      <input className={styles.search} aria-label="Search documents" placeholder="Search documents…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div className={styles.recentHead}>RECENT DOCUMENTS</div>
      <div className={styles.documents}>
        {libraryLoading && <div className={styles.emptyHistory}>LOADING…</div>}
        {libraryError && <div className={styles.libraryError} role="alert">{libraryError}<button className={styles.openButton} onClick={() => void refresh()}>RETRY</button></div>}
        {!libraryLoading && !libraryError && documents.length === 0 && <div className={styles.emptyHistory}>NO RECENT DOCUMENTS</div>}
        {documents.filter((doc) => doc.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())).map((doc) => <button key={doc.name} onClick={() => void open(doc.name)} className={[styles.docButton, name === doc.name ? styles.docButtonActive : ""].join(" ")} title={"Open " + doc.name}>
          <span className={styles.docName}>{doc.name.replace(/\.html$/i, "")}</span>
          <span className={styles.docDate}>{new Date(doc.modified_at * 1000).toLocaleString()}</span>
        </button>)}
      </div>
    </aside>}
    <main className={styles.main}>
      <div className={styles.topbar}>
        <button className={styles.openButton} onClick={() => { setLibraryOpen(true); void refresh(); }} aria-expanded={libraryOpen}>RECENT DOCUMENTS</button>
        <input className={styles.title} disabled={loadingDocument} value={title} onChange={(e) => { revision.current += 1; setTitle(e.target.value); setDirty(true); }} aria-label="Document title" />
        <button className={styles.saveButton} onClick={() => void save()} disabled={saving}>{saving ? "SAVING…" : "SAVE"}</button>
        <button className={styles.downloadButton} onClick={() => void download()} disabled={saving}>HTML ↓</button>
        <button className={styles.downloadButton} onClick={exportText}>TXT ↓</button>
        <button className={styles.openButton} disabled={saving} onClick={() => importRef.current?.click()}>IMPORT TXT</button>
        <input ref={importRef} type="file" accept=".txt,.md,text/plain" hidden onChange={(event) => {
          void importText(event.target.files?.[0]); event.target.value = "";
        }} />
      </div>
      <div className={styles.toolbar} role="toolbar" aria-label="Text formatting">
        {tools.map(([label, cmd, value]) => <button key={`${cmd}${value || ""}`} className={styles.tool} title={cmd} aria-label={cmd} onMouseDown={(e) => e.preventDefault()} onClick={() => command(cmd, value)}>{label}</button>)}
        <button className={styles.tool} title="Insert link" aria-label="Insert link" onMouseDown={(e) => e.preventDefault()} onClick={link}>🔗</button>
      </div>
      <div className={styles.editorWrap}>
        <div ref={editorRef} className={styles.editor} contentEditable={!loadingDocument} suppressContentEditableWarning spellCheck role="textbox" aria-label="Document content" aria-multiline="true"
          onDrop={(event) => event.preventDefault()}
          onPaste={(event) => {
            event.preventDefault();
            document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
            changed();
          }}
          onInput={changed} />
      </div>
      <div className={styles.status} role="status">{text.trim() ? text.trim().split(/\s+/).length : 0} words · {text.length} characters · {dirty ? "UNSAVED CHANGES · " : ""}{status}</div>
    </main>
  </div>;
}
