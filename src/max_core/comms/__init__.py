"""Communication channels for MAX: email, WhatsApp and phone calls.

Providers are chosen for India:
* Email - any SMTP/IMAP mailbox (e.g. Gmail with an app password).
* WhatsApp - Meta's official WhatsApp Cloud API (no unofficial clients).
* Phone - Exotel (Indian cloud telephony; Twilio cannot do India-to-India calls,
  see docs/TELEPHONY_ARCHITECTURE.md).

Every outbound message or call is an EFFECTFUL tool (owner approves each one)
and may only go to a contact the owner added. Inbound messages and calls are
stored as untrusted data and are never treated as instructions.
"""

from max_core.comms.store import CommsStore, Contact, InboundItem
from max_core.comms.tools import comms_tools

__all__ = ["CommsStore", "Contact", "InboundItem", "comms_tools"]
