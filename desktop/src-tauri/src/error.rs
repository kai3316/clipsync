use serde::Serialize;

#[derive(Clone, Debug, Serialize)]
pub struct BridgeError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

impl BridgeError {
    pub fn new(code: &str, message: &str) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            retryable: false,
        }
    }

    pub fn unavailable() -> Self {
        Self::new(
            "SIDECAR_UNAVAILABLE",
            "ClipSync background process is unavailable",
        )
    }
}

/// The updater plugin's errors carry a sentence and a cause, not a code, so
/// they collapse into one: what the caller does about a missing manifest and
/// about a signature that does not verify is the same — fall back to the
/// manual path — and the sentence is what the card shows.
impl From<tauri_plugin_updater::Error> for BridgeError {
    fn from(err: tauri_plugin_updater::Error) -> Self {
        Self::new("UPDATE_ERROR", &err.to_string())
    }
}
