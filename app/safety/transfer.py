"""Redirect a verified active Twilio call to the tenant's staff destination."""

import re
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from app.calls.service import CallContext
from app.config.settings import Settings

if TYPE_CHECKING:
    from app.voice.twilio import TwilioCredentials


class TransferError(RuntimeError):
    pass


def transfer_twiml(number: str) -> str:
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
        raise ValueError("Invalid transfer destination")
    root = Element("Response")
    dial = SubElement(root, "Dial", {"timeout": "20"})
    SubElement(dial, "Number").text = number
    SubElement(
        root, "Say"
    ).text = "We could not connect you to staff. Please contact the business directly."
    SubElement(root, "Hangup")
    return tostring(root, encoding="unicode")


async def transfer_call(
    settings: Settings,
    context: CallContext,
    number: str,
    *,
    client: httpx.AsyncClient | None = None,
    credentials: "TwilioCredentials | None" = None,
) -> None:
    from app.voice.twilio import TwilioCredentials

    # Tenant calls pass their own connection; the platform account is legacy only.
    account = credentials or TwilioCredentials.from_settings(settings)
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", context.provider_call_id) or not re.fullmatch(
        r"AC[0-9a-fA-F]{32}", account.account_sid
    ):
        raise ValueError("Invalid provider call identifier")
    twiml = transfer_twiml(number)
    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{account.account_sid}/Calls/{context.provider_call_id}.json"
    )
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=5, follow_redirects=False)
    try:
        response = await http.post(
            url,
            auth=httpx.BasicAuth(*account.rest_auth),
            data={"Twiml": twiml},
        )
        if response.status_code not in {200, 201}:
            raise TransferError("Provider rejected call transfer")
    except httpx.HTTPError:
        raise TransferError("Provider call transfer failed") from None
    finally:
        if own_client:
            await http.aclose()
