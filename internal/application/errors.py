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
#
# One sentence per code is right for a failure that has one reason to be
# refused -- there is nothing else AICONFIG_UNAVAILABLE can mean, and a reader
# who sees it needs no more than the fact.  It is the wrong shape for the codes
# that answer for a dozen actions each: NO FILES TO SEND and INVALID GROUP NAME
# are both INVALID_ARGUMENT, and one sentence covering both describes neither.
# Those are MESSAGE_KEYS' business.
ERROR_KEYS: dict[str, str] = {
    "APP_LOCKED": "error.app_locked",
    "APP_NOT_READY": "error.app_not_ready",
    "NOT_FOUND": "error.not_found",
    "NOT_CONNECTED": "error.not_connected",
    "NOT_PAIRED": "error.not_paired",
    "NOT_SUPPORTED": "error.not_supported",
    "INVALID_ARGUMENT": "error.invalid_argument",
    "VALIDATION_ERROR": "error.validation_error",
    "DATA_INVALID": "error.data_invalid",
    "INVALID_URL": "error.invalid_url",
    "LAN_NOT_RUNNING": "error.lan_not_running",
    "LAN_NOT_READY": "error.lan_not_ready",
    "LAN_OPERATION_FAILED": "error.lan_operation_failed",
    "LAN_START_FAILED": "error.lan_start_failed",
    "INTERNET_SYNC_OFF": "error.internet_sync_off",
    "RELAY_OFFLINE": "error.relay_offline",
    "INTERNET_PAIRING_UNAVAILABLE": "error.internet_pairing_unavailable",
    "INVALID_PAIRING_CODE": "error.invalid_pairing_code",
    "INVALID_PASSWORD": "error.invalid_password",
    "INVALID_NAME": "error.invalid_name",
    "SAVE_FAILED": "error.save_failed",
    "COPY_FAILED": "error.copy_failed",
    "SEND_FAILED": "error.send_failed",
    "OPEN_FAILED": "error.open_failed",
    "REVEAL_FAILED": "error.reveal_failed",
    "CLIPBOARD_WRITE_FAILED": "error.clipboard_write_failed",
    "EXPORT_FAILED": "error.export_failed",
    "IMPORT_FAILED": "error.import_failed",
    "STORAGE_ERROR": "error.storage_error",
    "AICONFIG_UNAVAILABLE": "error.aiconfig_unavailable",
    "TRANSLATE_FAILED": "error.translate_failed",
    "COMPANION_START_FAILED": "error.companion_start_failed",
    "COMPANION_STOP_FAILED": "error.companion_stop_failed",
    "COMPANION_STOPPING": "error.companion_stopping",
    "RESET_FAILED": "error.reset_failed",
    "RESTORE_PARTIAL_FAILED": "error.restore_partial_failed",
    "INTERNAL_ERROR": "error.internal_error",
    "DATA_IN_USE": "error.data_in_use",
    "update.offer_failed": "error.update_offer_failed",
    "update.fetch_failed": "error.update_fetch_failed",
    "update.no_asset": "error.update_no_asset",
    "update.peer_unreachable": "error.update_peer_unreachable",
}

# The sentences a code cannot tell apart, keyed by the sentence itself.
#
# INVALID_ARGUMENT answers for every refused argument in the app, and NOT_FOUND
# for every row that has gone since the list was drawn.  The one sentence each
# of those could be given would be the same shrug for a group name, a URL
# scheme and an empty folder -- true, and no help at all when the reader is
# owed the reason.  So the messages a reader can actually reach are spelled
# here exactly as their raise site writes them, and keep their own sentence.
#
# Keyed on the message rather than on an identifier passed down from the raise
# site, which is the shell's own convention: desktop/src/i18n/en.ts is keyed by
# the Chinese source string for the same reason.  A sentence already exists at
# the site that knows what went wrong; making it carry a key as well would buy
# nothing but a second place to keep in step.
#
# The price of keying on prose is that editing a message silently drops its
# translation.  The fallback is what makes that affordable: an unmatched
# message still gets its code's sentence, so a stale entry here costs precision
# and never correctness -- and the raise site's own words stay in the log,
# where they were always the point.
MESSAGE_KEYS: dict[str, str] = {
    # INVALID_ARGUMENT — one per refusal a reader can bring about themselves.
    "Invalid group name": "error.invalid_group_name",
    "Title or content is required": "error.favorite_empty",
    "Text is required": "error.text_required",
    "Text is too long": "error.text_too_long",
    "Invalid URL": "error.invalid_url",
    "Only http(s) URLs can be sent": "error.url_scheme",
    "No files to send": "error.no_files_to_send",
    "That folder has no files to send": "error.folder_empty",
    "No entry to download": "error.no_entry_to_download",
    "Only received files can be opened": "error.only_received_files",
    "Transfer has no saved file": "error.transfer_no_file",
    "Transfer cannot be retried": "error.transfer_not_retryable",
    "Unsupported export format": "error.unsupported_export_format",
    "no relay brokers configured": "error.no_relay_brokers",
    # NOT_FOUND — a row or a device the list still shows and the app no longer
    # has.  Which one it was is the whole of what the reader needs to know.
    "Device not found": "error.device_gone",
    "History item no longer exists": "error.history_item_gone",
    "Favorite no longer exists": "error.favorite_gone",
    "Internet peer not found": "error.internet_peer_gone",
    "Unknown transfer": "error.transfer_gone",
    "The file is no longer on disk": "error.file_gone",
    "Received file is not available": "error.received_file_gone",
    "Removed device not found": "error.removed_device_gone",
    "No certificate change is pending": "error.no_certificate_change",
    # NOT_SUPPORTED — the code's own sentence is about this platform not having
    # the feature at all, which is the wrong answer for a row that is merely
    # somewhere else.
    "This file is on the device that published it — download it first":
        "error.file_on_other_device",
    # VALIDATION_ERROR — the certificate-change flow's own refusal.
    "The device must connect again before it can be trusted":
        "error.device_must_reconnect",
}


def error_message(code: str, fallback: str) -> str:
    """The message for *code* in the active locale, *fallback* when it has none.

    The code is what a caller acts on and what a front end keys its own wording
    by; the message is what a reader sees.  A missing entry falls back rather
    than surfacing a key, so a code added in one place and not the other reads
    as an English sentence instead of as ``error.something_new``.

    The message's own entry is asked for first: it is the more specific of the
    two, and the only one that can tell two failures of one code apart.  Each
    is asked in turn rather than the first being taken outright, so a sentence
    whose catalog entry has gone missing falls back to its code's wording
    instead of all the way to English.
    """
    for key in (MESSAGE_KEYS.get(fallback), ERROR_KEYS.get(code)):
        if key is None:
            continue
        text = T(key)
        # An unresolved key comes back as itself; that is the missing entry.
        if text != key:
            return text
    return fallback


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
