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
