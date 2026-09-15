use serde::ser::SerializeStruct;
use serde::{Serialize, Serializer};

use crate::i18n;

/// Which side of the IPC raised a failure.
///
/// The code alone cannot answer it.  `VALIDATION_ERROR`, `NOT_FOUND`,
/// `OPEN_FAILED` and `INTERNAL_ERROR` are raised on both sides and mean
/// different things on each, and the sidecar writes its sentence in the user's
/// own language (`internal/application/errors.py`) with a detail this process
/// never had — a rejected chat action, a row that has gone.  So the error
/// records where it came from, and that is what decides whether the host's
/// table may word it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin {
    /// Raised here, with an English sentence from this process's source.
    Host,
    /// Raised by the sidecar, with the sentence that side already worded.
    Sidecar,
}

#[derive(Clone, Debug)]
pub struct BridgeError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    origin: Origin,
}

impl BridgeError {
    pub fn new(code: &str, message: &str) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            retryable: false,
            origin: Origin::Host,
        }
    }

    /// A failure the host reports with no reason of its own to give.
    ///
    /// Empty rather than a sentence: the words are [`i18n`]'s, and a message
    /// here would be a second copy of them in one language, which is how the
    /// band came to show English under a Chinese interface.
    pub fn unavailable() -> Self {
        Self::new("SIDECAR_UNAVAILABLE", "")
    }

    /// A failure the sidecar raised, from the `error` object of its reply.
    pub(crate) fn from_sidecar(value: &serde_json::Value) -> Self {
        Self {
            code: value["code"].as_str().unwrap_or("INTERNAL_ERROR").into(),
            message: value["message"]
                .as_str()
                .unwrap_or("Operation failed")
                .into(),
            retryable: value["retryable"].as_bool().unwrap_or(false),
            origin: Origin::Sidecar,
        }
    }

    /// Replace the message, keeping the code, the flag and the origin.
    ///
    /// The origin has to travel: `bridge::explain` swaps in the sidecar's last
    /// words for a host failure, and an error that forgot it came from the host
    /// would then be passed through untranslated.
    pub fn with_message(mut self, message: String) -> Self {
        self.message = message;
        self
    }

    /// The message the window should draw, in the language it is drawn in.
    ///
    /// This is the one place a host failure becomes words.  Both routes to the
    /// window go through it — the `Serialize` impl below, which carries a
    /// rejected command, and `bridge::terminate`'s `sidecar:state` event — so a
    /// sentence cannot be translated on one and not the other.
    pub fn localized_message(&self) -> String {
        match self.origin {
            Origin::Host => i18n::host_error(&self.code, &self.message),
            Origin::Sidecar => self.message.clone(),
        }
    }
}

impl Serialize for BridgeError {
    /// Writes the message in the user's language rather than in this file's.
    ///
    /// Hand-written for that one field: `serde` has no hook that could reach
    /// the locale, and deriving here meant every failure this host raises
    /// reached the failure band as English wording on a Chinese screen.
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut out = serializer.serialize_struct("BridgeError", 3)?;
        out.serialize_field("code", &self.code)?;
        out.serialize_field("message", &self.localized_message())?;
        out.serialize_field("retryable", &self.retryable)?;
        out.end()
    }
}

/// The updater plugin's errors carry a sentence and a cause, not a code, so
/// they collapse into one: what the caller does about a missing manifest and
/// about a signature that does not verify is the same — fall back to the
/// manual path — and the sentence is what the card shows.
///
/// The plugin words that sentence in English and this is not the place to read
/// it apart, so it rides along as the failure's detail, under a sentence of
/// ours that says what was being attempted.
impl From<tauri_plugin_updater::Error> for BridgeError {
    fn from(err: tauri_plugin_updater::Error) -> Self {
        Self::new("UPDATE_ERROR", &err.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Hold the process-wide locale for one test (see `i18n`'s test module).
    fn locale() -> std::sync::MutexGuard<'static, ()> {
        crate::i18n::tests::locale()
    }

    #[test]
    fn a_rejected_command_carries_a_sentence_the_window_can_read() {
        let _held = locale();
        let error = BridgeError::new("VALIDATION_ERROR", "Invalid chat action");
        // This is the whole of the bug: the band draws `error.message`, and it
        // used to draw the English the host wrote.
        assert_eq!(
            serde_json::to_value(&error).unwrap(),
            json!({
                "code": "VALIDATION_ERROR",
                "message": "请求的内容不符合预期，本次操作没有执行。",
                "retryable": false,
            })
        );
    }

    #[test]
    fn a_failure_the_sidecar_worded_is_not_worded_again() {
        let _held = locale();
        // Same code, other side, and this is why the code is not the test: the
        // sidecar knows which field was wrong and has already said so in the
        // user's language.  Rewriting it here would replace that with a
        // generic sentence about a request that "was not in the expected form".
        let error = BridgeError::from_sidecar(&json!({
            "code": "VALIDATION_ERROR",
            "message": "聊天文件操作不合法",
            "retryable": false,
        }));
        assert_eq!(
            serde_json::to_value(&error).unwrap()["message"],
            "聊天文件操作不合法"
        );
    }

    #[test]
    fn the_state_event_and_the_rejection_agree_on_the_sentence() {
        let _held = locale();
        // `bridge::terminate` emits the message by hand rather than through
        // `Serialize`, which is a second place a sentence could be translated
        // on one route and left in English on the other.
        let error = BridgeError::unavailable();
        assert_eq!(
            error.localized_message(),
            serde_json::to_value(&error).unwrap()["message"]
        );
        assert!(error.localized_message().starts_with("ClipSync 后台进程不可用。"));
    }

    #[test]
    fn the_updaters_own_sentence_survives_under_ours() {
        let _held = locale();
        let error = BridgeError::new("UPDATE_ERROR", "signature verification failed");
        assert_eq!(
            error.localized_message(),
            "检查更新失败：signature verification failed"
        );
    }
}
