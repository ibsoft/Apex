/* Delivery of the panel and chat-input commands to the mounted chat panel.

   These commands act on state the chat panel owns (which tab is showing, what
   is in the input box), while the provider is the one that decides a command
   was given. The provider could not simply set the values itself, and it must
   not write to a second copy of them: two copies of "what tab is open" drift,
   and the user only ever sees one of them.

   So the command is delivered as a *cancelable* event, exactly like the Notepad
   bridge, and the panel acknowledges it:

   * `preventDefault()` means the panel accepted the command, and the retry loop
     stops. A command issued while the panel is not mounted yet is therefore not
     lost, which is the failure mode a plain "write to a ref, read it in an
     effect" has - the effect only runs on a render, and a second spoken command
     does not cause one.
   * `respond` returns the acknowledgement string the voice layer speaks, so
     "write in chat" cannot claim success unless the text really reached the box.

   The timeout is a safety net, not the normal path: if nothing ever mounts (a
   signed-out session, a crash in the panel) the caller gets a truthful message
   instead of hanging.
 */
export type PanelTabName = "chat" | "history" | "settings" | "memory" | "apps" | "tasks";

/** Every name the panel actually renders, for validating a `?panel=` deep link
 *  (see the manifest shortcut in public/manifest.webmanifest). A hand-kept copy
 *  of this list would drift, and a drifted one would acknowledge a command it
 *  cannot perform. */
export const PANEL_TAB_NAMES: readonly PanelTabName[] = [
  "chat",
  "history",
  "settings",
  "memory",
  "apps",
  "tasks",
];

export type PanelCommand = {
  action: "open" | "close" | "toggle" | "maximize" | "normalize";
  tab?: PanelTabName;
};
export type ChatInputCommand = { action: "write" | "send"; text?: string };

export const PANEL_EVENT = "apex:panel";
export const CHAT_INPUT_EVENT = "apex:chat-input";

const RETRY_MS = 50;
const TIMEOUT_MS = 30000;

/** Resolve once a consumer accepts, or with `timeoutMessage` if none appears. */
function deliver(eventName: string, detail: object, timeoutMessage: string): Promise<string> {
  return new Promise((resolve) => {
    let accepted = false;
    const timeout = setTimeout(() => {
      clearInterval(retry);
      resolve(timeoutMessage);
    }, TIMEOUT_MS);
    const respond = (message: string) => {
      clearTimeout(timeout);
      clearInterval(retry);
      // A tick of delay lets the panel finish its own state update before the
      // acknowledgement is spoken, so the two never race.
      setTimeout(() => resolve(message), 0);
    };
    const attempt = () => {
      if (accepted) return;
      const event = new CustomEvent(eventName, { cancelable: true, detail: { ...detail, respond } });
      accepted = !window.dispatchEvent(event);
      if (accepted) clearInterval(retry);
    };
    const retry = setInterval(attempt, RETRY_MS);
    attempt();
  });
}

export function sendPanelCommand(command: PanelCommand): Promise<string> {
  return deliver(
    PANEL_EVENT,
    { ...command },
    "The chat panel is not available right now.",
  );
}

export function sendChatInputCommand(command: ChatInputCommand): Promise<string> {
  return deliver(
    CHAT_INPUT_EVENT,
    { ...command },
    "The chat panel is not available right now.",
  );
}

/** Split on a leading command word: "write in chat: hello" -> "hello". */
export function stripCommandPrefix(text: string, commandText: string): string {
  if (!text.startsWith(commandText)) return text;
  return text.slice(commandText.length).replace(/^[\s:,\-–]+/, "");
}
