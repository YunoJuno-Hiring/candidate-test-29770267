"""Rendering invoices into documents.

An invoice's billed content lives in Invoice and InvoiceLine. This module
turns that into a file in a given format, which is stored as an
InvoiceDocument and attached to the invoice emails.

To add a format (for example PDF, or a UBL e-invoice):
  1. Write a renderer that takes an Invoice and returns a RenderedDocument.
  2. Register it in RENDERERS and add the format to InvoiceDocument.FORMAT_CHOICES.
  3. Set BILLING_INVOICE_DOCUMENT_FORMAT in settings to the new format.
Nothing else in billing needs to change.
"""

import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.template.loader import render_to_string

from .models import InvoiceDocument

DEFAULT_FORMAT = InvoiceDocument.FORMAT_TXT


@dataclass(frozen=True)
class RenderedDocument:
    content: bytes
    content_type: str
    extension: str


def render_invoice_text(invoice):
    period = invoice.billing_period
    text = render_to_string('billing/documents/invoice.txt', {
        'invoice': invoice,
        'lines': invoice.lines.all(),
        'company': period.company,
        'freelancer': invoice.freelancer,
        'month': period.month,
    })
    # Start with a UTF-8 byte-order mark. Once the file is saved or opened
    # outside the email, its charset header is gone, and without the BOM many
    # viewers assume Windows-1252 and show '£' as 'Â£'.
    return RenderedDocument(content=text.encode('utf-8-sig'), content_type='text/plain', extension='txt')


RENDERERS = {
    InvoiceDocument.FORMAT_TXT: render_invoice_text,
}


def invoice_document_format():
    return getattr(settings, 'BILLING_INVOICE_DOCUMENT_FORMAT', DEFAULT_FORMAT)


def create_invoice_document(invoice, format=None):
    """Render `invoice` in `format` (the configured format by default) and store it."""
    format = format or invoice_document_format()
    try:
        renderer = RENDERERS[format]
    except KeyError:
        raise ValueError(f'No invoice renderer for format {format!r}.') from None

    rendered = renderer(invoice)
    return InvoiceDocument.objects.create(
        invoice=invoice,
        format=format,
        filename=f'{invoice.number}.{rendered.extension}',
        content_type=rendered.content_type,
        content=rendered.content,
        size=len(rendered.content),
        sha256=hashlib.sha256(rendered.content).hexdigest(),
    )
