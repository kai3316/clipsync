"""Transport-independent application failures."""

from internal.i18n import T

# The codes whose wire message is a sentence this app owns, mapped to the
# catalog key holding it.  A raiser writes its message in English because it is
# writing for a log; this table is where that becomes something a Chinese
# window can show.
#
# A code that is absent here keeps the message its raiser wrote.  That is the
# point of keying on the code rather than replacing every message at every
# raise site: 184 of them raise, most carry a sentence no reader ever sees
# (INVALID_RESULT and RESPONSE_TOO_LARGE are about the IPC profile, not about
# anything a person did), and the ones that do reach a reader are exactly the
# ones worth a translation.
ERROR_KEYS: dict[str, str] = {
    "APP_LOCKED": "error.app_locked",
    "NOT_FOUND": "error.not_found",
    "NOT_CONNECTED": "error.not_connected",
    "NOT_PAIRED": "error.not_paired",
    "NOT_SUPPORTED": "error.not_supported",
    "LAN_NOT_RUNNING": "error.lan_not_running",
    "LAN_NOT_READY": "error.lan_not_ready",
    "LAN_OPERATION_FAILED": "error.lan_operation_failed",
    "INTERNET_SYNC_OFF": "error.internet_sync_off",
    "INVALID_PAIRING_CODE": "error.invalid_pairing_code",
    "INVALID_PASSWORD": "error.invalid_password",
    "INVALID_NAME": "error.invalid_name",
    "SAVE_FAILED": "error.save_failed",
    "COPY_FAILED": "error.copy_failed",
    "SEND_FAILED": "error.send_failed",
    "OPEN_FAILED": "error.open_failed",
    "REVEAL_FAILED": "error.reveal_failed",
    "EXPORT_FAILED": "error.export_failed",
    "IMPORT_FAILED": "error.import_failed",
    "STORAGE_ERROR": "error.storage_error",
    "DATA_IN_USE": "error.data_in_use",
    "update.offer_failed": "error.update_offer_failed",
    "update.peer_unreachable": "error.update_peer_unreachable",
}


def error_message(code: str, fallback: str) -> str:
    """The message for *code* in the active locale, *fallback* when it has none.

    The code is what a caller acts on and what a front end keys its own wording
    by; the message is what a reader sees.  A missing entry falls back rather
    than surfacing a key, so a code added in one place and not the other reads
    as an English sentence instead of as ``error.something_new``.
    """
    key = ERROR_KEYS.get(code)
    if key is None:
        return fallback
    text = T(key)
    return fallback if text == key else text


class ApplicationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": error_message(self.code, self.message),
            "retryable": self.retryable,
        }
