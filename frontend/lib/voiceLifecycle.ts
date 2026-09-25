/** Complete silent/failed commands without interrupting speech or a newer turn. */
export async function settleVoiceCommand(
  run: () => void | Promise<void>,
  stillProcessing: () => boolean,
  resume: () => void,
  reportError: (error: unknown) => void,
): Promise<void> {
  try {
    await run();
  } catch (error) {
    if (stillProcessing()) reportError(error);
  } finally {
    if (stillProcessing()) resume();
  }
}
