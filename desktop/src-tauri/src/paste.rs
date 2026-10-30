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
    use std::ffi::c_void;
    use std::sync::atomic::{AtomicBool, Ordering};

    // Injection goes through CoreGraphics only.  The old osascript route needed
    // two separate permissions — Automation (ClipSync → System Events) *and*
    // Accessibility — and its failure mode was the raw “not authorised to send
    // Apple events” / “osascript is not allowed to send keystrokes” error.  The
    // CGEvent path needs Accessibility alone, and macOS shows the standard
    // prompt that also puts ClipSync in 系统设置 → 隐私与安全性 → 辅助功能.
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> u8;
        fn AXIsProcessTrustedWithOptions(options: *const c_void) -> u8;
        fn CGEventCreateKeyboardEvent(
            source: *mut c_void,
            virtual_key: u16,
            key_down: u8,
        ) -> *mut c_void;
        fn CGEventSetFlags(event: *mut c_void, flags: u64);
        fn CGEventPost(tap: u32, event: *mut c_void);
        static kAXTrustedCheckOptionPrompt: *const c_void;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: *const c_void);
        fn CFDictionaryCreate(
            allocator: *const c_void,
            keys: *const *const c_void,
            values: *const *const c_void,
            num_values: isize,
            key_callbacks: *const c_void,
            value_callbacks: *const c_void,
        ) -> *const c_void;
        static kCFBooleanTrue: *const c_void;
    }

    const COMMAND_FLAG: u64 = 1 << 20; // kCGEventFlagMaskCommand
    const KEY_V: u16 = 9; // kVK_ANSI_V

    /// Whether the system prompt has already been asked for in this process.
    /// Asking on every paste would stack dialogs; the first one lists ClipSync
    /// in the settings pane, and the error sentence then tells the reader where
    /// to flip the switch.
    static PROMPTED: AtomicBool = AtomicBool::new(false);

    fn permission_hint() -> String {
        "自动粘贴需要 macOS「辅助功能」权限：请打开「系统设置 → 隐私与安全性 → 辅助功能」，\
         把 ClipSync 的开关打开。第一次触发粘贴时系统会弹出授权提示并把它列进这个列表；\
         如果刚才没有看到提示，再触发一次粘贴即可，打开开关后重试。"
            .to_string()
    }

    /// Ask macOS for Accessibility once (the option dictionary is what makes it
    /// show the standard “ClipSync would like to control this computer” prompt
    /// and register the app in the list).  A plain `AXIsProcessTrusted` check —
    /// what the first version did — asks for nothing, which is exactly why the
    /// app was missing from the settings pane.
    fn accessibility_trusted() -> bool {
        if unsafe { AXIsProcessTrusted() } != 0 {
            return true;
        }
        if PROMPTED.swap(true, Ordering::SeqCst) {
            return false;
        }
        unsafe {
            let keys = [kAXTrustedCheckOptionPrompt];
            let values = [kCFBooleanTrue];
            let options = CFDictionaryCreate(
                std::ptr::null(),
                keys.as_ptr(),
                values.as_ptr(),
                1,
                std::ptr::null(),
                std::ptr::null(),
            );
            if options.is_null() {
                return false;
            }
            let _ = AXIsProcessTrustedWithOptions(options);
            CFRelease(options);
        }
        // The dialog is asynchronous: the first attempt is still untrusted and
        // the reader is told to switch the row on and retry.
        unsafe { AXIsProcessTrusted() != 0 }
    }

    /// Post Cmd+V through the HID event tap.
    fn post_paste() -> Result<(), String> {
        unsafe {
            for key_down in [1u8, 0u8] {
                let event = CGEventCreateKeyboardEvent(std::ptr::null_mut(), KEY_V, key_down);
                if event.is_null() {
                    return Err("无法创建按键事件（辅助功能权限或系统状态异常）".to_string());
                }
                CGEventSetFlags(event, COMMAND_FLAG);
                CGEventPost(0, event); // kCGHIDEventTap
                CFRelease(event as *const c_void);
            }
        }
        Ok(())
    }

    /// macOS keeps no foreground token: reading one would need an Apple Event
    /// (Automation) and the CGEvent paste needs only Accessibility.  Hiding the
    /// overlay is what gives the focus back to the window behind it.
    pub fn record() -> Option<Foreground> {
        None
    }

    pub fn restore(_foreground: &Foreground) -> Result<(), String> {
        Ok(())
    }

    pub fn inject() -> Result<(), String> {
        if !accessibility_trusted() {
            return Err(permission_hint());
        }
        post_paste()
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
