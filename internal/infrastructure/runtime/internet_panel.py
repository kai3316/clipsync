"""Host adapter for the phone panel's internet pairing + delivery routes.

``internal/web/api/internetpair.py`` and ``internal/web/api/internetdelivery.py``
are bind-style branches: ``routes.py`` hands the request to their ``handle``,
which delegates to a host object bound once at startup.  Legacy bound the
Application (``src/main.py``); nothing bound them under the sidecar, so both
routes answered 503 ("internet pairing unavailable" / "internet delivery
unavailable") and the phone's internet panel and delivery badges were dead
buttons.

This adapter is that host object.  It wears the legacy ``_netpair_*`` /
``_delivery_*`` surface and returns ``(data_dict, status_code)`` — and it never
raises: the routes wrap ``handle`` in a bare ``except Exception`` → 500, so an
:class:`ApplicationError` has to be translated here into the status the panel
already knows how to show.
"""

from __future__ import annotations

import logging

from internal.application.errors import ApplicationError
from internal.transport.relay import probe_relay_endpoints

logger = logging.getLogger(__name__)

# Application failure → HTTP status the legacy handler answered with.  The
# phone's panel branches on the status, not on the text: 400 for anything the
# user can correct, 503 for "the relay link this needs is not up".
_ERROR_STATUS = {
    "INTERNET_SYNC_OFF": 400,
    "INVALID_PAIRING_CODE": 400,
    "INVALID_NAME": 400,
    "NOT_FOUND": 400,
    "SAVE_FAILED": 500,
    "RELAY_OFFLINE": 503,
    "INTERNET_PAIRING_UNAVAILABLE": 503,
}

# The panel shows these strings verbatim (the relay test renders the error as
# its result summary), so keep the legacy wording rather than the internal one.
_LEGACY_MESSAGES = {
    "INTERNET_SYNC_OFF": "internet sync is off",
    "INVALID_PAIRING_CODE": "invalid pairing code",
    "INVALID_NAME": "invalid name",
    "NOT_FOUND": "unknown peer",
    "RELAY_OFFLINE": "relay not connected",
    "INTERNET_PAIRING_UNAVAILABLE": "internet pairing unavailable",
}


class InternetPanelHost:
    """The legacy Application surface those two routes were written against."""

    def __init__(self, runtime, probe=probe_relay_endpoints):
        """Adapt the sidecar runtime for the phone routes.

        ``probe`` is injectable so the relay test can be exercised without
        opening sockets.
        """
        self.runtime = runtime
        self.config = runtime.config
        self._probe = probe

    # ---------------------------------------------------- internet pairing

    def _service(self):
        service = getattr(self.runtime, "internet_pairing", None)
        if service is None:
            raise ApplicationError(
                "INTERNET_PAIRING_UNAVAILABLE", "Internet pairing is not configured"
            )
        return service

    def _netpair_generate(self):
        try:
            data = self._service().generate()
        except ApplicationError as exc:
            return self._failure(exc)
        return {"ok": True, "code": data["code"]}, 200

    def _netpair_enter(self, code):
        try:
            data = self._service().enter(code)
        except ApplicationError as exc:
            return self._failure(exc)
        return {"ok": True, "peer_id": data["peer_id"]}, 200

    def _netpair_status(self, now=None):
        try:
            data = self._service().status()
        except ApplicationError as exc:
            return self._failure(exc)
        # ``waiting`` rides along for the same reason the desktop card shows it:
        # a code entered here whose partner has not answered is neither a peer
        # nor nothing, and a panel that drops the key has no answer to "did it
        # connect?" between the submit and the hello.  The phone's own panel
        # does not render it yet — it is passed through so the answer is on the
        # wire, not invented at the far end.
        return {
            "generated_code": data["generated_code"],
            "peers": data["peers"],
            "waiting": data.get("waiting", []),
        }, 200

    def _netpair_rename(self, peer_id, name=None):
        try:
            self._service().rename(peer_id, name)
        except ApplicationError as exc:
            return self._failure(exc)
        return {"ok": True}, 200

    def _netpair_unpair(self, peer_id):
        try:
            # The unpair publishes the phone's ``netpair_peer`` {unpaired} push
            # itself (see LanRuntime._internet_unpaired), so sibling tabs learn
            # about it no matter which caller severed the pair.
            self._service().unpair(peer_id)
        except ApplicationError as exc:
            return self._failure(exc)
        return {"ok": True}, 200

    def _netpair_test(self, body=None):
        """Probe the relay brokers the panel staged (or the saved list)."""
        brokers = None
        if isinstance(body, dict):
            candidate = body.get("brokers")
            if isinstance(candidate, list):
                brokers = [b.strip() for b in candidate if isinstance(b, str) and b.strip()]
        if not brokers:
            brokers = [b for b in (getattr(self.config, "relay_brokers", None) or []) if b]
        if not brokers:
            return {"ok": False, "error": "no relay brokers configured"}, 400
        try:
            results = self._probe(brokers)
        except Exception:
            # probe_relay_endpoints promises not to raise; anything that does
            # is a crash the panel should see as a failed test, not a 500 with
            # no result body.
            logger.exception("relay broker probe failed")
            return {"ok": False, "error": "relay test failed"}, 500
        reachable = sum(1 for row in results if row.get("ok"))
        return {
            "ok": True,
            "results": results,
            "summary": f"{reachable}/{len(brokers)} reachable",
        }, 200

    # --------------------------------------------------- internet delivery

    def _delivery_status(self, peer_id=""):
        """The panel's delivery view: the ledger's rows under its ``sends`` key."""
        view = self.runtime.relay_delivery_status(peer_id)
        return {"pending": view["pending"], "sends": view["items"]}

    def _delivery_counts(self):
        return self.runtime.delivery_counts()

    # ----------------------------------------------------------------- utils

    def _failure(self, exc):
        logger.debug("Internet panel request refused: %s", exc.code)
        message = _LEGACY_MESSAGES.get(exc.code, exc.message)
        return {"ok": False, "error": message}, _ERROR_STATUS.get(exc.code, 500)
