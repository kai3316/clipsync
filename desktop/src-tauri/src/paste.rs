//! Native paste injection, and the hand-off back to the application a window
//! of ours took the focus from.
//!
//! Both new 1.3.0 entry points need the same three steps: write the clip to the
//! clipboard (the sidecar does that over `history.copy`), give the focus back to
//! the application the user was in, and press Ctrl/Cmd+V there.
//!
//! * the quick-paste window hides itself and pastes into the application that
//!   had the focus before the window opened;
//! * the history row's "paste to current app" action hides the main window and
//!   pastes into whatever window the window manager activates next.
//! * the global `paste_N` shortcuts paste into whichever application is already
//!   in front (no window of ours is showing at that moment), so they only need
//!   the key injection.
//!
//! The platform halves are deliberately thin: an opaque [`Foreground`] token
//! recorded before our window takes the focus, and a fixed Ctrl/Cmd+V.  A
//! failure here is never fatal for the clip — it is already on the clipboard —
//! so every error carries a sentence the window can fall back to ("press the
//! shortcut yourself"), which is also what the legacy desktop did when the
//! platform refused the keystroke.

use std::time::Duration;

/// The application that had the focus when one of our windows took it.
///
/// Opaque on purpose: Windows stores a window handle, macOS an application
/// name, Linux an X11 window id.  The command layer only stores and hands it
/// back; nothing above this module interprets it.
#[derive(Clone, Debug)]
pub struct Foreground {
    raw: String,
}

/// Record who has the focus right now, if the platform can say.
///
/// `None` is a normal answer (a Wayland compositor with no X11 window id, a
/// macOS process without Accessibility permission): the caller then pastes
/// into whatever the window manager activates, or reports "paste manually".
pub fn record_foreground() -> Option<Foreground> {
    platform::record()
}

/// Press Ctrl/Cmd+V in the application that currently has the focus.
///
/// `delay` is spent before the keystroke so the caller can hide its own window
/// first; see [`restore_and_paste`] for the variant that gives the focus back.
pub fn inject_paste(delay: Duration) -> Result<(), String> {
    if !delay.is_zero() {
        std::thread::sleep(delay);
    }
    platform::inject()
}

/// Give the focus back to *foreground*, wait for it to settle, then paste.
///
/// A missing `foreground` is not a hard failure: hiding our window is usually
/// enough for the window manager to re-activate the previous application, so
/// the keystroke is still attempted.
pub fn restore_and_paste(foreground: Option<Foreground>, delay: Duration) -> Result<(), String> {
    if let Some(foreground) = foreground {
        platform::restore(&foreground)?;
        // The keystroke must not race the focus change; 120 ms is the same
        // order CrossPaste waits for the target to come back to the front.
        std::thread::sleep(Duration::from_millis(120));
    }
    if !delay.is_zero() {
        std::thread::sleep(delay);
    }
    platform::inject()
}

#[cfg(target_os = "windows")]
mod platform {
    use super::Foreground;
    use std::time::{Duration, Instant};
    use windows::Win32::Foundation::HWND;
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP,
        VIRTUAL_KEY, VK_CONTROL, VK_V,
    };
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, SetForegroundWindow};

    fn hwnd_of(foreground: &Foreground) -> Result<HWND, String> {
        let value: usize = foreground
            .raw
            .parse()
            .map_err(|_| "无法解析前台窗口句柄".to_string())?;
        Ok(HWND(value as *mut _))
    }

    fn wait_for_foreground(target: HWND, timeout: Duration) -> bool {
        let started = Instant::now();
        while started.elapsed() < timeout {
            if unsafe { GetForegroundWindow().0 } == target.0 {
                return true;
            }
            std::thread::sleep(Duration::from_millis(8));
        }
        unsafe { GetForegroundWindow().0 == target.0 }
    }

    pub fn record() -> Option<Foreground> {
        let hwnd = unsafe { GetForegroundWindow() };
        if hwnd.0.is_null() {
            return None;
        }
        Some(Foreground {
            raw: (hwnd.0 as usize).to_string(),
        })
    }

    pub fn restore(foreground: &Foreground) -> Result<(), String> {
        let target = hwnd_of(foreground)?;
        // `SetForegroundWindow` is allowed here because the user just acted in
        // one of our windows, which is one of the documented conditions; the
        // retry covers the window manager taking a moment to finish the hand-off.
        for _ in 0..3 {
            let _ = unsafe { SetForegroundWindow(target) };
            if wait_for_foreground(target, Duration::from_millis(250)) {
                return Ok(());
            }
        }
        Err("无法切回上一个应用".to_string())
    }

    fn key(vk: VIRTUAL_KEY, up: bool) -> INPUT {
        let flags: KEYBD_EVENT_FLAGS = if up {
            KEYEVENTF_KEYUP
        } else {
            KEYBD_EVENT_FLAGS(0)
        };
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    pub fn inject() -> Result<(), String> {
        // One SendInput call for the whole chord, released in reverse order, so
        // no partial state is left behind while the target handles the key.
        let inputs = [
            key(VK_CONTROL, false),
            key(VK_V, false),
            key(VK_V, true),
            key(VK_CONTROL, true),
        ];
        let sent = unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) };
        if sent == inputs.len() as u32 {
            Ok(())
        } else {
            Err("系统拒绝了按键注入（可能被安全软件拦截）".to_string())
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use super::Foreground;
    use std::process::Command;

    fn osascript(script: &str) -> Result<String, String> {
        let output = Command::new("osascript")
            .arg("-e")
            .arg(script)
            .output()
            .map_err(|error| format!("无法运行 osascript：{error}"))?;
        if !output.status.success() {
            let reason = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(if reason.is_empty() {
                "系统拒绝了按键注入".to_string()
            } else {
                reason
            });
        }
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    pub fn record() -> Option<Foreground> {
        let name = osascript(
            "tell application \"System Events\" to get name of first application process whose frontmost is true",
        )
        .ok()?;
        if name.is_empty() {
            None
        } else {
            Some(Foreground { raw: name })
        }
    }

    pub fn restore(foreground: &Foreground) -> Result<(), String> {
        let name = foreground.raw.replace('\\', "\\\\").replace('"', "\\\"");
        osascript(&format!(
            "tell application \"System Events\" to set frontmost of process \"{name}\" to true"
        ))
        .map(|_| ())
    }

    pub fn inject() -> Result<(), String> {
        osascript("tell application \"System Events\" to keystroke \"v\" using command down")
            .map(|_| ())
    }
}

#[cfg(all(unix, not(target_os = "macos")))]
mod platform {
    use super::Foreground;
    use std::process::Command;

    /// Wayland has no global window id to focus, and its security model keeps
    /// one application from activating another; there the window manager
    /// re-focuses the previous application when our window hides and the
    /// keystroke is injected with `wtype`.
    fn on_wayland() -> bool {
        std::env::var_os("WAYLAND_DISPLAY").is_some()
    }

    fn run(program: &str, args: &[&str]) -> Result<String, String> {
        let output = Command::new(program)
            .args(args)
            .output()
            .map_err(|_| format!("{program} 不可用"))?;
        if !output.status.success() {
            let reason = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(if reason.is_empty() {
                format!("{program} 执行失败")
            } else {
                reason
            });
        }
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    pub fn record() -> Option<Foreground> {
        if on_wayland() {
            return None;
        }
        let id = run("xdotool", &["getactivewindow"]).ok()?;
        if id.is_empty() {
            None
        } else {
            Some(Foreground { raw: id })
        }
    }

    pub fn restore(foreground: &Foreground) -> Result<(), String> {
        if on_wayland() {
            return Ok(());
        }
        let id = foreground.raw.as_str();
        run("xdotool", &["windowactivate", "--sync", id]).map(|_| ())
    }

    pub fn inject() -> Result<(), String> {
        if on_wayland() {
            run("wtype", &["-M", "ctrl", "v", "-m", "ctrl"]).map(|_| ())
        } else {
            run("xdotool", &["key", "--clearmodifiers", "ctrl+v"]).map(|_| ())
        }
    }
}

#[cfg(not(any(target_os = "windows", target_os = "macos", unix)))]
mod platform {
    use super::Foreground;

    pub fn record() -> Option<Foreground> {
        None
    }

    pub fn restore(_foreground: &Foreground) -> Result<(), String> {
        Err("当前平台不支持自动粘贴".to_string())
    }

    pub fn inject() -> Result<(), String> {
        Err("当前平台不支持自动粘贴".to_string())
    }
}
