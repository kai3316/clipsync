//! Strings for the native surfaces the renderer cannot reach: the tray menu and
//! the failures the host raises in its own right.
//!
//! The shell has its own layer (`desktop/src/i18n`, Chinese source strings);
//! this is the Rust-side counterpart for the handful of labels the host draws
//! itself.  The locale always comes from the sidecar's saved `language`, so an
//! unknown or missing value falls back to the source language instead of
//! leaving a native surface blank.

use serde_json::Value;
use std::sync::Mutex;

/// Locales the native surfaces can render.  Mirrors `desktop/src/i18n/index.ts`.
pub const LOCALES: [&str; 2] = ["zh-CN", "en"];
pub const DEFAULT_LOCALE: &str = "zh-CN";

/// Every native-surface string, in one language.
#[derive(Debug, PartialEq, Eq)]
pub struct Strings {
    /// Tray menu labels.
    pub tray: Tray,
    /// Save-dialog filter for a history export (JSON/CSV).
    pub filter_history: &'static str,
    /// Save-dialog filter for a backup archive.
    pub filter_backup: &'static str,
    /// Save-dialog filter for the log file.
    pub filter_log: &'static str,
    /// The sentences for failures this host raises itself.
    pub errors: Errors,
}

/// The failures the host reports in words of its own, one per code.
///
/// Every code here is one `BridgeError` raises with a sentence this process
/// wrote, and each reached the window as English under a Chinese interface
/// until this table existed — the failure band draws `state.error.message`
/// directly, and nothing between the two translated it.
///
/// A code the sidecar sends is *not* here and must not be: those carry the
/// sentence `internal/application/errors.py` already worded in the user's
/// language, and a table entry for one of the names the two sides share
/// (`VALIDATION_ERROR`, `NOT_FOUND`, `OPEN_FAILED`, `INTERNAL_ERROR`) would
/// overwrite a sentence naming what actually went wrong with a generic one.
/// [`host_error`] tells the two apart by which side raised the error, not by
/// the code, which is why the overlap is safe.
#[derive(Debug, PartialEq, Eq)]
pub struct Errors {
    /// A sentence's variable part goes where `{detail}` stands, and a sentence
    /// that carries none keeps none of the message.  `{reason}` is the same
    /// slot for the sidecar's own last words, which are quoted with a label —
    /// [`Errors::reason_detail`] — and dropped whole when there are none, so a
    /// sidecar that died quietly does not leave an empty label behind.
    pub validation_error: &'static str,
    pub protocol_error: &'static str,
    pub protocol_mismatch: &'static str,
    pub permission_denied: &'static str,
    pub busy: &'static str,
    pub not_found: &'static str,
    pub open_failed: &'static str,
    pub window_error: &'static str,
    pub request_timeout: &'static str,
    pub recovery_timeout: &'static str,
    pub recovery_failed: &'static str,
    pub autostart_failed: &'static str,
    pub autostart_unavailable: &'static str,
    pub autostart_rollback_failed: &'static str,
    pub sidecar_unavailable: &'static str,
    pub sidecar_start_failed: &'static str,
    pub startup_timeout: &'static str,
    pub update_error: &'static str,
    /// What this language calls the sidecar's own last words (`{detail}`).
    pub reason_detail: &'static str,
}

impl Errors {
    /// This code's sentence, or `None` for a code this host did not raise.
    fn sentence(&self, code: &str) -> Option<&'static str> {
        Some(match code {
            "VALIDATION_ERROR" => self.validation_error,
            "PROTOCOL_ERROR" => self.protocol_error,
            "PROTOCOL_MISMATCH" => self.protocol_mismatch,
            "PERMISSION_DENIED" => self.permission_denied,
            "BUSY" => self.busy,
            "NOT_FOUND" => self.not_found,
            "OPEN_FAILED" => self.open_failed,
            "WINDOW_ERROR" => self.window_error,
            "REQUEST_TIMEOUT" => self.request_timeout,
            "RECOVERY_TIMEOUT" => self.recovery_timeout,
            "RECOVERY_FAILED" => self.recovery_failed,
            "AUTOSTART_FAILED" => self.autostart_failed,
            "AUTOSTART_UNAVAILABLE" => self.autostart_unavailable,
            "AUTOSTART_ROLLBACK_FAILED" => self.autostart_rollback_failed,
            "SIDECAR_UNAVAILABLE" => self.sidecar_unavailable,
            "SIDECAR_START_FAILED" => self.sidecar_start_failed,
            "STARTUP_TIMEOUT" => self.startup_timeout,
            "UPDATE_ERROR" => self.update_error,
            _ => return None,
        })
    }

    /// Fill a sentence's slot with the message's variable part.
    fn fill(&self, template: &str, detail: &str) -> String {
        let detail = detail.trim();
        if let Some((head, tail)) = template.split_once("{reason}") {
            if detail.is_empty() {
                return format!("{head}{tail}");
            }
            let quoted = self.reason_detail.replace("{detail}", detail);
            return format!("{head}{quoted}{tail}");
        }
        match template.split_once("{detail}") {
            Some((head, tail)) => format!("{head}{detail}{tail}"),
            None => template.to_owned(),
        }
    }
}

/// Tray menu labels, one field per entry.
///
/// The legacy tray carried these same entries, but six of its catalog keys did
/// not exist (`tray.sync`, `tray.pause_for`, `tray.pause_15m` / `_30m` / `_1h`,
/// `tray.paused_left`, `tray.resume_now`), so it rendered those literal key
/// strings.  The words here are the ones the dashboard and this window already
/// use for the same actions.
#[derive(Debug, PartialEq, Eq)]
pub struct Tray {
    /// Disabled header naming this device (`{name}`).
    pub device: &'static str,
    /// The sync checkbox.
    pub sync: &'static str,
    /// The pause submenu while no deadline is armed.
    pub pause_for: &'static str,
    pub pause_15m: &'static str,
    pub pause_30m: &'static str,
    pub pause_1h: &'static str,
    /// The pause submenu's title while a deadline is armed (`{minutes}`).
    pub paused_left: &'static str,
    /// Ends that pause now.
    pub resume_now: &'static str,
    pub show: &'static str,
    pub send_url: &'static str,
    pub show_web_qr: &'static str,
    /// The peers submenu while at least one device is connected (`{count}`).
    pub devices_counted: &'static str,
    /// The same entry while none is.
    pub devices: &'static str,
    /// Its single row when no device is known at all.
    pub no_devices: &'static str,
    /// One device row (`{name}`, `{state}`).
    pub device_entry: &'static str,
    /// The states a device row can carry, in the words the devices page uses.
    pub state_connected: &'static str,
    pub state_offline: &'static str,
    pub state_pairing: &'static str,
    pub state_found: &'static str,
    pub settings: &'static str,
    pub export_logs: &'static str,
    pub check_update: &'static str,
    pub about: &'static str,
    pub quit: &'static str,
}

/// The source language: the strings as they were written before this module.
///
/// Two of them were changed on 2026-09-12 so that the tray says what the window
/// says, since they are the same app's two native surfaces.  A peer's state word
/// was 已连接, which is the *window's* word for the app's own link to its engine
/// (the dot in its header) — the window calls that peer 在线, and the file that
/// draws these rows already claimed to use the devices page's four words.  And
/// the send entry was 发送链接到设备 while this same table's English said "Send
/// URL to Device" and the window's button says 发送网址: the tray's own two
/// languages disagreed before the window was brought into it.
const ZH: Strings = Strings {
    tray: Tray {
        device: "设备：{name}",
        sync: "同步",
        pause_for: "定时暂停同步",
        pause_15m: "15 分钟",
        pause_30m: "30 分钟",
        pause_1h: "1 小时",
        paused_left: "已暂停 · 剩余 {minutes} 分钟",
        resume_now: "立即恢复",
        show: "显示 ClipSync",
        send_url: "发送网址到设备",
        show_web_qr: "显示网页二维码",
        devices_counted: "已连接设备 ({count})",
        devices: "已连接设备",
        no_devices: "无已连接设备",
        device_entry: "{name}（{state}）",
        state_connected: "在线",
        state_offline: "离线",
        state_pairing: "等待确认",
        state_found: "已发现",
        settings: "设置...",
        export_logs: "导出日志...",
        check_update: "检查更新",
        about: "关于 ClipSync",
        quit: "退出 ClipSync",
    },
    filter_history: "历史导出",
    filter_backup: "ClipSync 备份",
    filter_log: "日志文件",
    errors: Errors {
        // The ones the reader can only report: they mean this window sent the
        // host something it should not have, so the sentence says the click
        // did nothing rather than pretending to explain a field name.
        validation_error: "请求的内容不符合预期，本次操作没有执行。",
        protocol_error: "窗口与后台进程之间的数据无法解析。",
        protocol_mismatch: "后台进程的接口版本与这个窗口不一致，请重新安装 ClipSync。",
        permission_denied: "这个操作只能从主窗口发起。",
        busy: "正在进行的操作太多，请稍后重试。",
        // The ones the reader can do something about.
        not_found: "要打开的接收文件已经不在了。",
        open_failed: "无法打开这个文件，可能被其他程序占用。",
        window_error: "无法最小化窗口。",
        request_timeout: "后台进程没有在时限内答复，操作结果未知。请刷新后再试一次。",
        recovery_timeout: "数据修复没有在时限内完成。",
        recovery_failed: "后台进程在报告结果之前就退出了。",
        autostart_failed: "无法更改开机自启设置。",
        autostart_unavailable: "开机自启只在安装版中可用。",
        autostart_rollback_failed: "开机自启设置失败，且没能恢复原来的设置。",
        // The process itself failed.  The two that quote the sidecar's last
        // words keep their diagnosis: it is what a bug report needs, and the
        // words are the sidecar's own either way.
        sidecar_unavailable: "ClipSync 后台进程不可用。{reason}",
        sidecar_start_failed: "无法启动后台进程（{detail}）。",
        startup_timeout: "后台进程没有在时限内就绪。{reason}",
        update_error: "检查更新失败：{detail}",
        reason_detail: "原因：{detail}",
    },
};

const EN: Strings = Strings {
    tray: Tray {
        device: "Device: {name}",
        sync: "Sync",
        pause_for: "Pause sync for",
        pause_15m: "15 minutes",
        pause_30m: "30 minutes",
        pause_1h: "1 hour",
        paused_left: "Paused · {minutes} min left",
        resume_now: "Resume now",
        show: "Show ClipSync",
        send_url: "Send URL to Device",
        show_web_qr: "Show Web QR Code",
        devices_counted: "Connected Devices ({count})",
        devices: "Connected Devices",
        no_devices: "No connected devices",
        device_entry: "{name} ({state})",
        state_connected: "Online",
        state_offline: "Offline",
        state_pairing: "Awaiting confirmation",
        state_found: "Discovered",
        settings: "Settings...",
        export_logs: "Export Logs...",
        check_update: "Check for Updates",
        about: "About ClipSync",
        quit: "Quit ClipSync",
    },
    filter_history: "History export",
    filter_backup: "ClipSync backup",
    filter_log: "Log files",
    errors: Errors {
        validation_error: "The request was not in the form this build expects, so nothing was done.",
        protocol_error: "A frame between this window and the background process could not be read.",
        protocol_mismatch: "The background process speaks a different interface version. Reinstall ClipSync.",
        permission_denied: "This operation has to be started from the main window.",
        busy: "Too many operations are already in flight. Try again in a moment.",
        not_found: "The received file is no longer there.",
        open_failed: "Could not open the file — another program may be holding it.",
        window_error: "Could not minimize the window.",
        request_timeout: "The background process did not answer in time, so the result is unknown. Refresh before trying again.",
        recovery_timeout: "Data recovery did not finish in time.",
        recovery_failed: "The background process exited before reporting a result.",
        autostart_failed: "Could not change the launch-at-login setting.",
        autostart_unavailable: "Launch at login is only available in an installed build.",
        autostart_rollback_failed: "Launch at login failed, and the previous setting could not be restored.",
        sidecar_unavailable: "The ClipSync background process is unavailable. {reason}",
        sidecar_start_failed: "Could not start the background process ({detail}).",
        startup_timeout: "The background process did not become ready in time. {reason}",
        update_error: "Could not check for updates: {detail}",
        reason_detail: "Reason: {detail}",
    },
};

/// Coerce anything the sidecar may hand us to a supported locale.
pub fn normalize(locale: &str) -> &'static str {
    LOCALES
        .into_iter()
        .find(|known| *known == locale)
        .unwrap_or(DEFAULT_LOCALE)
}

/// The strings for a locale; an unknown locale gets the source language.
pub fn strings(locale: &str) -> &'static Strings {
    match normalize(locale) {
        "en" => &EN,
        _ => &ZH,
    }
}

/// The saved `language` from a `settings.get` response (`{"settings": {…}}`).
pub fn locale_from_settings(settings: &Value) -> &str {
    settings["settings"]["language"]
        .as_str()
        .unwrap_or(DEFAULT_LOCALE)
}

/// The locale last applied to the native surfaces.
///
/// The locale lives in a process-wide cell rather than in a value the app
/// manages, because the readers are not all in a position to be handed one: a
/// command can ask the app for state, but `Serialize` cannot, and the failures
/// this host raises are worded through it.  [`crate::tray::apply`] is what
/// keeps it in step with the tray, and it starts at the source language so a
/// surface drawn before the first settings read is worded rather than blank.
static CURRENT: Mutex<String> = Mutex::new(String::new());

/// The locale in force, or the source language while nothing has set one.
fn current() -> &'static str {
    match CURRENT.lock() {
        Ok(locale) => normalize(&locale),
        Err(_) => DEFAULT_LOCALE,
    }
}

/// The locale last applied to the native surfaces, as managed state.
///
/// A handle and nothing more: the locale itself is [`CURRENT`], so a caller
/// that can reach the app and one that can only reach a `&self` read the same
/// value.
#[derive(Default)]
pub struct CurrentLocale;

impl CurrentLocale {
    /// Set the locale, reporting whether it actually changed.
    ///
    /// The caller needs to know: re-rendering a native menu that is already in
    /// the right language means rewriting every label under a menu the user may
    /// have open, for nothing.
    pub fn set(&self, locale: &str) -> bool {
        let next = normalize(locale);
        if let Ok(mut current) = CURRENT.lock() {
            if current.as_str() == next {
                return false;
            }
            *current = next.to_owned();
            return true;
        }
        false
    }

    /// The strings for the current locale.  A poisoned lock reads as the source
    /// language rather than panicking a dialog open.
    pub fn strings(&self) -> &'static Strings {
        strings(current())
    }

    /// The tray labels alone.  A dynamic tray label (the device name, the pause
    /// countdown) is rendered from lines drawn in this language, so the menu
    /// needs the table rather than one finished string.
    pub fn tray(&self) -> &'static Tray {
        &self.strings().tray
    }
}

/// The sentence for a failure this host raised, in the current language.
///
/// For a code the host did not raise this answers `message` unchanged — the
/// sidecar words its own failures in this same language, and four of the codes
/// the two sides share ([`Errors`] names them) mean different things on each.
/// The code therefore cannot be what tells them apart, and the caller is the
/// one that knows: [`crate::error::BridgeError::localized_message`] is the only
/// caller, and it asks the error's own record of which side raised it.
pub fn host_error(code: &str, message: &str) -> String {
    let errors = &strings(current()).errors;
    match errors.sentence(code) {
        Some(template) => errors.fill(template, message),
        None => message.to_owned(),
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use serde_json::json;

    /// Serialises the tests that move the process-wide locale.
    ///
    /// `cargo test` runs these on threads in one process, so one test's `set`
    /// is another's ambient language — and the assertion that a sentence is
    /// Chinese is worth nothing if a test beside it left the cell on `en`.
    static LOCALE: Mutex<()> = Mutex::new(());

    /// Take the process-wide locale for one test, reset to the source language.
    ///
    /// A test that asserts what a sentence says has to know which language it
    /// is in, and with one cell for the process that means both holding it and
    /// starting from a known value.
    pub(crate) fn locale() -> std::sync::MutexGuard<'static, ()> {
        let held = LOCALE.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        CurrentLocale.set(DEFAULT_LOCALE);
        held
    }

    /// Every field, so the tests below cannot miss a new one.
    fn all(strings: &Strings) -> [&'static str; 46] {
        [
            strings.tray.device,
            strings.tray.sync,
            strings.tray.pause_for,
            strings.tray.pause_15m,
            strings.tray.pause_30m,
            strings.tray.pause_1h,
            strings.tray.paused_left,
            strings.tray.resume_now,
            strings.tray.show,
            strings.tray.send_url,
            strings.tray.show_web_qr,
            strings.tray.devices_counted,
            strings.tray.devices,
            strings.tray.no_devices,
            strings.tray.device_entry,
            strings.tray.state_connected,
            strings.tray.state_offline,
            strings.tray.state_pairing,
            strings.tray.state_found,
            strings.tray.settings,
            strings.tray.export_logs,
            strings.tray.check_update,
            strings.tray.about,
            strings.tray.quit,
            strings.filter_history,
            strings.filter_backup,
            strings.filter_log,
            strings.errors.validation_error,
            strings.errors.protocol_error,
            strings.errors.protocol_mismatch,
            strings.errors.permission_denied,
            strings.errors.busy,
            strings.errors.not_found,
            strings.errors.open_failed,
            strings.errors.window_error,
            strings.errors.request_timeout,
            strings.errors.recovery_timeout,
            strings.errors.recovery_failed,
            strings.errors.autostart_failed,
            strings.errors.autostart_unavailable,
            strings.errors.autostart_rollback_failed,
            strings.errors.sidecar_unavailable,
            strings.errors.sidecar_start_failed,
            strings.errors.startup_timeout,
            strings.errors.update_error,
            strings.errors.reason_detail,
        ]
    }

    /// The placeholders each template must keep: a translation that drops one
    /// would leave the user reading a label with no name or number in it, and
    /// the fill that follows has nothing to put there.
    #[test]
    fn every_template_keeps_its_placeholders() {
        for locale in LOCALES {
            let tray = &strings(locale).tray;
            assert!(tray.device.contains("{name}"), "{locale} lost {{name}}");
            assert!(
                tray.paused_left.contains("{minutes}"),
                "{locale} lost {{minutes}}"
            );
            assert!(
                tray.devices_counted.contains("{count}"),
                "{locale} lost {{count}}"
            );
            assert!(
                tray.device_entry.contains("{name}") && tray.device_entry.contains("{state}"),
                "{locale} lost a device-row placeholder"
            );
        }
    }

    /// The two error slots have to survive translation too, and they are the
    /// ones a translator can drop without the text reading wrong: a sentence
    /// with no `{reason}` still reads as a finished sentence, it has just
    /// thrown away the only account of why the process died.
    #[test]
    fn every_error_template_keeps_the_slot_it_fills() {
        let source = &ZH.errors;
        let english = &EN.errors;
        assert_eq!(source.reason_detail.matches("{detail}").count(), 1);
        assert_eq!(english.reason_detail.matches("{detail}").count(), 1);
        for code in [
            "SIDECAR_UNAVAILABLE",
            "SIDECAR_START_FAILED",
            "STARTUP_TIMEOUT",
            "UPDATE_ERROR",
        ] {
            let zh = source.sentence(code).expect("a host code with no sentence");
            let en = english.sentence(code).expect("a host code with no sentence");
            let slot = if zh.contains("{reason}") { "{reason}" } else { "{detail}" };
            assert!(en.contains(slot), "{code} lost {slot} in English");
        }
    }

    #[test]
    fn every_locale_covers_every_native_string() {
        for locale in LOCALES {
            let strings = strings(locale);
            for label in all(strings) {
                assert!(!label.trim().is_empty(), "{locale} has a blank string");
            }
        }
    }

    #[test]
    fn english_is_a_real_translation_and_unknown_locales_fall_back() {
        let source = strings(DEFAULT_LOCALE);
        let english = strings("en");
        for (zh, en) in all(source).into_iter().zip(all(english)) {
            assert_ne!(zh, en, "an English string still holds the source text");
            assert!(
                !en.chars().any(|c| ('\u{4e00}'..='\u{9fff}').contains(&c)),
                "English string {en:?} still contains Chinese"
            );
        }
        for unknown in ["", "fr", "en-US", "zh", "EN"] {
            assert_eq!(strings(unknown), source, "{unknown:?} did not fall back");
        }
        assert_eq!(normalize("en"), "en");
        assert_eq!(normalize("zh-CN"), "zh-CN");
    }

    #[test]
    fn current_locale_starts_at_the_source_and_tracks_set() {
        let _held = locale();
        let current = CurrentLocale::default();
        assert_eq!(current.strings(), &ZH);
        assert!(current.set("en"), "the first switch is a change");
        assert_eq!(current.strings(), &EN);
        // Only a real change is reported: the tray renders on true alone, and a
        // refresh that re-renders every label for nothing is what that avoids.
        assert!(!current.set("en"));
        // An unsupported value falls back rather than leaving a half-set state.
        assert!(current.set("fr"));
        assert_eq!(current.strings(), &ZH);
        assert!(!current.set("zh-CN"));
    }

    #[test]
    fn a_host_failure_is_worded_in_the_language_on_screen() {
        let _held = locale();
        let current = CurrentLocale::default();
        // English in, English out of the way: the bug this table exists for is
        // the band drawing `state.error.message`, which is this sentence.
        assert_eq!(
            host_error("VALIDATION_ERROR", "Invalid chat action"),
            "请求的内容不符合预期，本次操作没有执行。"
        );
        current.set("en");
        assert_eq!(
            host_error("BUSY", "Too many pending commands"),
            "Too many operations are already in flight. Try again in a moment."
        );
    }

    #[test]
    fn a_failure_that_quotes_the_sidecar_keeps_what_it_quoted() {
        let _held = locale();
        // The diagnosis is the sidecar's own words, and a bug report needs
        // them: localising the sentence must not be localising the log line.
        assert_eq!(
            host_error("SIDECAR_UNAVAILABLE", "ModuleNotFoundError: no module named internal"),
            "ClipSync 后台进程不可用。原因：ModuleNotFoundError: no module named internal"
        );
        assert_eq!(
            host_error("SIDECAR_START_FAILED", "/opt/ClipSync/clipsync-sidecar: No such file"),
            "无法启动后台进程（/opt/ClipSync/clipsync-sidecar: No such file）。"
        );
        // A sidecar that died quietly has nothing to quote, and the label goes
        // with it rather than standing over an empty slot.
        assert_eq!(
            host_error("STARTUP_TIMEOUT", ""),
            "后台进程没有在时限内就绪。"
        );
        assert_eq!(
            host_error("STARTUP_TIMEOUT", "   "),
            "后台进程没有在时限内就绪。"
        );
    }

    #[test]
    fn a_code_newer_than_this_table_still_says_something() {
        // A `BridgeError::new` added without a sentence here reads as the
        // sentence it was written with rather than as an empty band.
        let _held = locale();
        assert_eq!(host_error("SOMETHING_NEW", "whatever it said"), "whatever it said");
    }

    #[test]
    fn settings_get_response_contract_uses_nested_settings() {
        // internal/web/api/settings.py returns {"settings": result}, not result.
        assert_eq!(
            locale_from_settings(&json!({"settings": {"language": "en"}})),
            "en"
        );
        for invalid in [
            json!({"language": "en"}),
            json!({"settings": null}),
            json!({"settings": {"language": 42}}),
            json!({"settings": {}}),
            Value::Null,
        ] {
            assert_eq!(locale_from_settings(&invalid), DEFAULT_LOCALE);
        }
    }
}
