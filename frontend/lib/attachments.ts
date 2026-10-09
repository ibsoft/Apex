/* Attachments the operator adds to a chat turn.

   A document dropped on the chat composer is uploaded to the memory store
   (embedded and kept for recall); the backend also returns a bounded preview,
   which the client echoes back on the next message so the model can talk about
   the document straight away instead of waiting for a recall round-trip.

   The preview is operator data, and the prompt block is written so the model
   reads it as data. `buildDocumentContext` is the single place that decides the
   shape, so it is pure and unit-tested here rather than in the component. */

export type ChatAttachment = {
  /** Original file name, as shown on the chip. */
  name: string;
  /** How many chunks were embedded into long-term memory. */
  chunks: number;
  /** Bounded leading slice of the extracted text, echoed to the model. */
  preview: string;
};

/** The non-persisted prompt block for the attached documents, or "" if there
 *  is nothing usable to send. */
export function buildDocumentContext(attachments: ChatAttachment[]): string {
  const usable = attachments.filter((a) => a.preview && a.preview.trim());
  if (!usable.length) return "";
  return usable
    .map((a) => {
      const count = a.chunks === 1 ? "1 chunk" : `${a.chunks} chunks`;
      return `--- Attached document: ${a.name} (${count} in long-term memory) ---\n${a.preview.trim()}`;
    })
    .join("\n\n");
}

/** Build attachment chips from the memory-upload response, dropping failed
 *  files and keeping only the fields the composer needs. */
export function attachmentsFromUpload(
  files: { filename: string; chunks?: number; preview?: string; error?: string }[],
): ChatAttachment[] {
  return files
    .filter((f) => !f.error)
    .map((f) => ({
      name: f.filename,
      chunks: f.chunks ?? 0,
      preview: f.preview ?? "",
    }));
}
