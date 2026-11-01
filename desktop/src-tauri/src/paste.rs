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

/// Make sure our own application is visible again before the overlay opens.
///
/// On macOS a paste hides the application (`NSApp.hide`) so the target can take
/// the focus; without an explicit unhide the next overlay would be a window of
/// a still-hidden app.  Other platforms have nothing to do.
pub fn activate_app() {
    #[cfg(target_os = "macos")]
    platform::activate_app();
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
        // order macOS clipboard utilities wait for the target to come back to the front.
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
    use std::ffi::{c_void, CString};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::{Duration, Instant};

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
        fn CGEventSourceCreate(state: u32) -> *mut c_void;
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
    // Reading and re-activating the frontmost application through NSWorkspace /
    // NSRunningApplication.  This is what the previous “sleep and hope” version
    // was missing: hiding the panel does not make macOS hand the focus back on
    // its own schedule, so Cmd+V could be posted while ClipSync was still the
    // active application and the keystroke went nowhere.
    #[link(name = "AppKit", kind = "framework")]
    extern "C" {}
    #[link(name = "objc")]
    extern "C" {
        fn objc_getClass(name: *const std::os::raw::c_char) -> *mut c_void;
        fn sel_registerName(name: *const std::os::raw::c_char) -> *mut c_void;
        fn objc_msgSend();
        // These are called from a tokio blocking thread, which has no
        // autorelease pool of its own; NSWorkspace hands back autoreleased
        // objects, so the pool is opened around each ObjC call.
        fn objc_autoreleasePoolPush() -> *mut c_void;
        fn objc_autoreleasePoolPop(pool: *mut c_void);
    }

    type SendObj = unsafe extern "C" fn(*mut c_void, *mut c_void) -> *mut c_void;
    type SendPid = unsafe extern "C" fn(*mut c_void, *mut c_void) -> i32;
    type SendObjFromPid =
        unsafe extern "C" fn(*mut c_void, *mut c_void, i32) -> *mut c_void;
    type SendBoolFromOptions =
        unsafe extern "C" fn(*mut c_void, *mut c_void, usize) -> i8;
    type SendI8 = unsafe extern "C" fn(*mut c_void, *mut c_void, i8) -> i8;
    type SendVoidFromObj = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void);
    type SendVoidSelObjBool =
        unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *mut c_void, i8);

    // Command plus the left-hand device bit, as a hardware key would send.
    const COMMAND_FLAG: u64 = (1 << 20) | 0x08; // kCGEventFlagMaskCommand | device L Cmd
    const KEY_V: u16 = 9; // kVK_ANSI_V
    const ACTIVATE_ALL_WINDOWS_AND_IGNORE_OTHER_APPS: usize = 3;
    const HID_EVENT_TAP: u32 = 0;
    const COMBINED_SESSION_SOURCE: u32 = 0;
    const SETTLE_AFTER_FRONTMOST: Duration = Duration::from_millis(50);
    const MIN_ACTIVATION_SETTLE: Duration = Duration::from_millis(200);

    /// Whether the system prompt has already been asked for in this process.
    /// Asking on every paste would stack dialogs; the first one lists ClipSync
    /// in the settings pane, and the error sentence then tells the reader where
    /// to flip the switch.
    static PROMPTED: AtomicBool = AtomicBool::new(false);

    fn class(name: &str) -> *mut c_void {
        let name = CString::new(name).expect("class name");
        unsafe { objc_getClass(name.as_ptr()) }
    }

    fn selector(name: &str) -> *mut c_void {
        let name = CString::new(name).expect("selector name");
        unsafe { sel_registerName(name.as_ptr()) }
    }

    unsafe fn send(obj: *mut c_void, name: &str) -> *mut c_void {
        let send: SendObj = std::mem::transmute(objc_msgSend as *const ());
        send(obj, selector(name))
    }

    unsafe fn send_pid(obj: *mut c_void, name: &str) -> i32 {
        let send: SendPid = std::mem::transmute(objc_msgSend as *const ());
        send(obj, selector(name))
    }

    unsafe fn send_with(pid: i32, obj: *mut c_void, name: &str) -> *mut c_void {
        let send: SendObjFromPid = std::mem::transmute(objc_msgSend as *const ());
        send(obj, selector(name), pid)
    }

    unsafe fn send_options(obj: *mut c_void, name: &str, options: usize) -> i8 {
        let send: SendBoolFromOptions = std::mem::transmute(objc_msgSend as *const ());
        send(obj, selector(name), options)
    }

    unsafe fn send_void(obj: *mut c_void, name: &str, arg: *mut c_void) {
        let send: SendVoidFromObj = std::mem::transmute(objc_msgSend as *const ());
        send(obj, selector(name), arg)
    }

    /// A BOOL parameter is one byte, not an NSUInteger; mixing the two ABI
    /// signatures works by accident on some arm64 builds and not others.
    unsafe fn send_i8(obj: *mut c_void, name: &str, value: i8) -> i8 {
        let send: SendI8 = std::mem::transmute(objc_msgSend as *const ());
        send(obj, selector(name), value)
    }

    /// Run one selector on the AppKit main thread, synchronously.
    ///
    /// `performSelectorOnMainThread:withObject:waitUntilDone:YES` is public
    /// AppKit and avoids linking libdispatch — the first attempt used
    /// `dispatch_get_main_queue`, whose symbols are not exported on current
    /// macOS SDKs, and the macOS bundle failed at the final link step.
    unsafe fn perform_on_main(obj: *mut c_void, name: &str, arg: *mut c_void) {
        let send: SendVoidSelObjBool = std::mem::transmute(objc_msgSend as *const ());
        send(
            obj,
            selector("performSelectorOnMainThread:withObject:waitUntilDone:"),
            selector(name),
            arg,
            1,
        );
    }

    fn frontmost_pid() -> Option<i32> {
        unsafe {
            let workspace = send(class("NSWorkspace"), "sharedWorkspace");
            if workspace.is_null() {
                return None;
            }
            let app = send(workspace, "frontmostApplication");
            if app.is_null() {
                return None;
            }
            let pid = send_pid(app, "processIdentifier");
            (pid > 0).then_some(pid)
        }
    }

    fn with_autorelease_pool<T>(body: impl FnOnce() -> T) -> T {
        let pool = unsafe { objc_autoreleasePoolPush() };
        let result = body();
        unsafe { objc_autoreleasePoolPop(pool) };
        result
    }

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

    /// Post Cmd+V through the HID event tap with the same event source and
    /// device-bit flags.  Modifiers travel only as flags on the
    /// V key events, never as separate Command key events, so a user still
    /// holding Command is not left with a rewritten modifier state.
    fn post_paste() -> Result<(), String> {
        unsafe {
            let source = CGEventSourceCreate(COMBINED_SESSION_SOURCE);
            let mut failure: Option<String> = None;
            for key_down in [1u8, 0u8] {
                let event = CGEventCreateKeyboardEvent(source, KEY_V, key_down);
                if event.is_null() {
                    failure = Some(
                        "无法创建按键事件（辅助功能权限或系统状态异常）".to_string(),
                    );
                    break;
                }
                CGEventSetFlags(event, COMMAND_FLAG);
                CGEventPost(HID_EVENT_TAP, event);
                CFRelease(event as *const c_void);
            }
            if !source.is_null() {
                CFRelease(source);
            }
            match failure {
                Some(message) => Err(message),
                None => Ok(()),
            }
        }
    }

    /// Bring ClipSync back after a paste hid it, so the next panel opening is not
    /// a window of a still-hidden application.
    pub fn activate_app() {
        unsafe {
            let app = send(class("NSApplication"), "sharedApplication");
            if !app.is_null() {
                perform_on_main(app, "unhide:", std::ptr::null_mut());
            }
        }
    }

    pub fn record() -> Option<Foreground> {
        let pid = with_autorelease_pool(frontmost_pid)?;
        // The panel can be opened while ClipSync itself is frontmost (for
        // example a tray click on a machine where the main window was just
        // hidden); activating ourselves again would paste into no window.
        if pid == std::process::id() as i32 {
            return None;
        }
        Some(Foreground {
            raw: pid.to_string(),
        })
    }

    pub fn restore(foreground: &Foreground) -> Result<(), String> {
        let pid: i32 = foreground
            .raw
            .parse()
            .map_err(|_| "无法解析原来的前台应用".to_string())?;
        let started = Instant::now();
        unsafe {
            // Hide ClipSync on the AppKit main thread (synchronously); just
            // hiding the panel window left the application active with no key
            // window, and that is where Cmd+V went.
            let app = send(class("NSApplication"), "sharedApplication");
            if !app.is_null() {
                perform_on_main(app, "hide:", std::ptr::null_mut());
            }
            let target = send_with(
                pid,
                class("NSRunningApplication"),
                "runningApplicationWithProcessIdentifier:",
            );
            if target.is_null() {
                return Err("找不到原来的前台应用".to_string());
            }
            let _ = send_options(
                target,
                "activateWithOptions:",
                ACTIVATE_ALL_WINDOWS_AND_IGNORE_OTHER_APPS,
            );
        }
        // Wait until the target really is frontmost, then give its key window
        // time to accept input.  A fixed sleep alone guessed and was wrong for
        // slow applications; clipboard utilities wait the same way (≥200ms activation,
        // extra 50ms after frontmost).
        let deadline = Instant::now() + Duration::from_millis(1500);
        while Instant::now() < deadline {
            if with_autorelease_pool(frontmost_pid) == Some(pid) {
                let settle = MIN_ACTIVATION_SETTLE
                    .saturating_sub(started.elapsed())
                    .max(SETTLE_AFTER_FRONTMOST);
                std::thread::sleep(settle);
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(15));
        }
        Err(format!(
            "原来的前台应用没有重新获得焦点（目标 pid {pid}，当前前台 pid {:?}）",
            with_autorelease_pool(frontmost_pid)
        ))
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
