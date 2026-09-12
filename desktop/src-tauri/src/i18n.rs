//! Strings for the native surfaces the renderer cannot reach: the tray menu
//! and OS notifications.
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
    /// Body of the "incoming file transfer" notification.
    pub notify_transfer: &'static str,
    /// Body of the "a phone uploaded a file" notification.
    pub notify_file_received: &'static str,
    /// Body of the "new chat message" notification.
    pub notify_chat: &'static str,
    /// Body of the "a device wants to pair" notification (`{name}`, `{code}`).
    pub notify_pairing_request: &'static str,
    /// Body of the "a device connected" notification (`{name}`).
    pub notify_device_connected: &'static str,
    /// Body of the "a device disconnected" notification (`{name}`).
    pub notify_device_disconnected: &'static str,
    /// Save-dialog filter for a history export (JSON/CSV).
    pub filter_history: &'static str,
    /// Save-dialog filter for a backup archive.
    pub filter_backup: &'static str,
    /// Save-dialog filter for the log file.
    pub filter_log: &'static str,
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
    notify_transfer: "收到文件传输请求",
    notify_file_received: "收到文件",
    notify_chat: "收到新的聊天消息",
    notify_pairing_request: "设备 \"{name}\" 请求配对 — 代码：{code}",
    notify_device_connected: "{name} 已连接",
    notify_device_disconnected: "{name} 已断开",
    filter_history: "历史导出",
    filter_backup: "ClipSync 备份",
    filter_log: "日志文件",
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
    notify_transfer: "Incoming file transfer request",
    notify_file_received: "File received",
    notify_chat: "New incoming chat message",
    notify_pairing_request: "Device \"{name}\" wants to pair — code: {code}",
    notify_device_connected: "{name} is now connected",
    notify_device_disconnected: "{name} has disconnected",
    filter_history: "History export",
    filter_backup: "ClipSync backup",
    filter_log: "Log files",
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
/// For code paths that cannot await a settings read — the native file dialogs
/// are opened from a command, not from an event.  [`crate::tray::apply`] keeps
/// it in step with the tray.
#[derive(Default)]
pub struct CurrentLocale(Mutex<String>);

impl CurrentLocale {
    /// Set the locale, reporting whether it actually changed.
    ///
    /// The caller needs to know: re-rendering a native menu that is already in
    /// the right language means rewriting every label under a menu the user may
    /// have open, for nothing.
    pub fn set(&self, locale: &str) -> bool {
        let next = normalize(locale);
        if let Ok(mut current) = self.0.lock() {
            if current.as_str() == next {
                return false;
            }
            *current = next.to_owned();
            return true;
        }
        false
    }

    /// The strings for the current locale.  An unset or poisoned lock reads as
    /// the source language rather than panicking a dialog open.
    pub fn strings(&self) -> &'static Strings {
        match self.0.lock() {
            Ok(current) => strings(&current),
            Err(_) => strings(DEFAULT_LOCALE),
        }
    }

    /// The tray labels alone.  A dynamic tray label (the device name, the pause
    /// countdown) is rendered from lines drawn in this language, so the menu
    /// needs the table rather than one finished string.
    pub fn tray(&self) -> &'static Tray {
        &self.strings().tray
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Every field, so the two tests below cannot miss a new one.
    fn all(strings: &Strings) -> [&'static str; 33] {
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
            strings.notify_transfer,
            strings.notify_file_received,
            strings.notify_chat,
            strings.notify_pairing_request,
            strings.notify_device_connected,
            strings.notify_device_disconnected,
            strings.filter_history,
            strings.filter_backup,
            strings.filter_log,
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
            let pairing = strings(locale).notify_pairing_request;
            assert!(pairing.contains("{name}") && pairing.contains("{code}"));
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
