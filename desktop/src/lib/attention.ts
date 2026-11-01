import { inDesktop } from "../api/bridge";

/**
 * Ask the taskbar to draw attention to this window.
 *
 * The window closes to the tray, and a pairing request or an incoming file is a
 * question with a deadline: it used to arrive only in the notice stack of a
 * window nobody was looking at, and expire unanswered. The taskbar's own
 * attention signal — a flashing button on Windows, a bouncing icon elsewhere —
 * is the one channel the system gives an application that is running but not on
 * screen, and it needs no notification permission.
 *
 * A window that is already visible is not flashing at anybody: the notice stack
 * is on screen, and the signal would be noise. Every call is wrapped, because
 * the window surface may be absent (a plain browser, a test) or refused by the
 * capability list, and neither is an error worth reporting.
 */
export async function requestAttention(): Promise<void> {
  if (!inDesktop()) return;
  try {
    // Imported lazily: this module is pulled into the shell's startup path, and
    // the window API is only needed the moment something arrives.
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const win = getCurrentWindow();
    if (await win.isVisible()) return;
    await win.requestUserAttention(1);
  } catch { /* No window surface, or the capability is not granted. */ }
}
