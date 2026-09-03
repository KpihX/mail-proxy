"""Zimbra SOAP transport and first-class tag primitives."""

from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from ..config import AccountDef, api_timeout
from ..exceptions import MailProxyError

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
ZIMBRA_NS = "urn:zimbra"
ACCOUNT_NS = "urn:zimbraAccount"
MAIL_NS = "urn:zimbraMail"


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _parse_message_element(m: ET.Element) -> dict[str, str]:
    """Extract attributes + child elements from a Zimbra ``<m>`` element."""
    data = dict(m.attrib)
    # <su>subject text</su>
    su = next((c.text or "" for c in m if _local_name(c) == "su"), "")
    if su:
        data["subject"] = su
    # <e a="email" p="name" />  (first sender)
    e = next((c for c in m if _local_name(c) == "e"), None)
    if e is not None:
        data["from_address"] = e.get("a", "")
        data["from_name"] = e.get("p", "")
    return data


class ZimbraSOAPClient:
    """Authenticated Zimbra SOAP client built from a resolved mail account."""

    def __init__(self, account: AccountDef, endpoint: str | None = None) -> None:
        if not account.password:
            raise MailProxyError("Zimbra SOAP requires a resolved account password.")
        self.account = account
        self.endpoint = endpoint or f"https://{account.imap.host}/service/soap"
        self._token: str | None = None

    def _post(self, operation: ET.Element, token: str | None = None) -> ET.Element:
        envelope = ET.Element(f"{{{SOAP_NS}}}Envelope")
        if token:
            header = ET.SubElement(envelope, f"{{{SOAP_NS}}}Header")
            context = ET.SubElement(header, f"{{{ZIMBRA_NS}}}context")
            ET.SubElement(context, f"{{{ZIMBRA_NS}}}authToken").text = token
        body = ET.SubElement(envelope, f"{{{SOAP_NS}}}Body")
        body.append(operation)
        request = urllib.request.Request(
            self.endpoint,
            data=ET.tostring(envelope, encoding="utf-8", xml_declaration=True),
            method="POST",
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=api_timeout()) as response:
                return ET.fromstring(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MailProxyError(f"Zimbra SOAP HTTP {exc.code}: {body[:500]}") from exc
        except (OSError, ET.ParseError) as exc:
            raise MailProxyError(f"Zimbra SOAP request failed: {exc}") from exc

    def _authenticate(self) -> str:
        if self._token:
            return self._token
        operation = ET.Element(f"{{{ACCOUNT_NS}}}AuthRequest")
        ET.SubElement(
            operation, f"{{{ACCOUNT_NS}}}account", {"by": "name"}
        ).text = self.account.email
        ET.SubElement(
            operation, f"{{{ACCOUNT_NS}}}password"
        ).text = self.account.password
        response = self._post(operation)
        token = next(
            (item.text for item in response.iter() if _local_name(item) == "authToken"),
            None,
        )
        if not token:
            raise MailProxyError("Zimbra SOAP authentication returned no auth token.")
        self._token = token
        return token

    def call_xml(self, operation_xml: str) -> ET.Element:
        """Call any Zimbra SOAP operation XML after local authentication."""
        try:
            operation = ET.fromstring(operation_xml)
        except ET.ParseError as exc:
            raise MailProxyError(f"Invalid Zimbra SOAP operation XML: {exc}") from exc
        return self._post(operation, self._authenticate())

    def call(self, method: str, payload: str) -> dict[str, Any]:
        """Call arbitrary request XML and return transparent XML response data."""
        operation = ET.fromstring(payload)
        if _local_name(operation) != method:
            raise MailProxyError(
                "zimbra-soap method must match the XML operation element."
            )
        response = self.call_xml(payload)
        return {"method": method, "xml": ET.tostring(response, encoding="unicode")}

    def tags(self) -> list[dict[str, str]]:
        response = self.call_xml(f'<GetTagRequest xmlns="{MAIL_NS}"/>')
        return [
            dict(item.attrib) for item in response.iter() if _local_name(item) == "tag"
        ]

    def create_tag(self, name: str, color: int | None = None) -> dict[str, str]:
        attrs = {"name": name}
        if color is not None:
            attrs["color"] = str(color)
        operation = ET.Element(f"{{{MAIL_NS}}}CreateTagRequest")
        ET.SubElement(operation, f"{{{MAIL_NS}}}tag", attrs)
        response = self.call_xml(ET.tostring(operation, encoding="unicode"))
        tag = next(
            (item for item in response.iter() if _local_name(item) == "tag"), None
        )
        if tag is None:
            raise MailProxyError("Zimbra SOAP CreateTagRequest returned no tag.")
        return dict(tag.attrib)

    def delete_tags(self, tag_ids: list[str]) -> None:
        joined = ",".join(tag_ids)
        self.call_xml(
            f'<ItemActionRequest xmlns="{MAIL_NS}"><action id="{joined}" op="delete"/></ItemActionRequest>'
        )

    def tag_items(self, tag_ids: list[str], item_ids: list[str], add: bool) -> None:
        operation = "tag" if add else "!tag"
        joined_items = ",".join(item_ids)
        for tag_id in tag_ids:
            self.call_xml(
                f'<ItemActionRequest xmlns="{MAIL_NS}"><action id="{joined_items}" op="{operation}" tag="{tag_id}"/></ItemActionRequest>'
            )

    def items(self, item_ids: list[str]) -> list[dict[str, str]]:
        """Resolve native item IDs and return full message metadata.

        Returns dicts with attributes (id, d, t, su…) plus child elements
        ``subject`` (from ``<su>``) and ``from_address`` (from ``<e a=…>``).
        """
        items: list[dict[str, str]] = []
        for item_id in item_ids:
            response = self.call_xml(
                f'<GetMsgRequest xmlns="{MAIL_NS}"><m id="{item_id}"/></GetMsgRequest>'
            )
            message = next(
                (item for item in response.iter() if _local_name(item) == "m"), None
            )
            if message is None:
                raise MailProxyError(f"Zimbra item does not exist: {item_id}")
            items.append(_parse_message_element(message))
        return items

    def tagged_items(self, tag_name: str) -> list[dict[str, str]]:
        """List message summaries associated with one native tag name."""
        operation = ET.Element(f"{{{MAIL_NS}}}SearchRequest", {"types": "message"})
        ET.SubElement(operation, f"{{{MAIL_NS}}}query").text = f'tag:"{tag_name}"'
        response = self.call_xml(ET.tostring(operation, encoding="unicode"))
        return [
            _parse_message_element(item)
            for item in response.iter()
            if _local_name(item) == "m"
        ]
