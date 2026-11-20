from django.conf import settings
from django.db import models

from accounts.models import Company, Freelancer
from contracts.models import TimesheetEntry


class BillingPeriod(models.Model):
    """The billing state of one company for one calendar month.

    A period is incomplete while any freelancer is on hold (they had hours
    for the month that were still draft or submitted when billing ran).
    """

    STATUS_INCOMPLETE = 'incomplete'
    STATUS_COMPLETE = 'complete'
    STATUS_CHOICES = [
        (STATUS_INCOMPLETE, 'Incomplete'),
        (STATUS_COMPLETE, 'Complete'),
    ]

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='billing_periods')
    month = models.DateField(help_text='First day of the billed calendar month.')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_INCOMPLETE)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['company', 'month'], name='unique_billing_period'),
        ]
        ordering = ['-month']

    def __str__(self):
        return f'{self.company.name} {self.month:%Y-%m} ({self.status})'


class BillingRun(models.Model):
    """An audit record of each time billing was run for a period."""

    billing_period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name='runs')
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    invoices_generated = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class BillingHold(models.Model):
    """A freelancer whose invoice for a period is waiting on unreviewed hours."""

    billing_period = models.ForeignKey(BillingPeriod, on_delete=models.CASCADE, related_name='holds')
    freelancer = models.ForeignKey(Freelancer, on_delete=models.PROTECT, related_name='billing_holds')
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When admins were told this freelancer is ready to be re-billed.',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['billing_period', 'freelancer'], name='unique_billing_hold'),
        ]


class Invoice(models.Model):
    """One invoice per company, freelancer and month.

    Uniqueness on (billing_period, freelancer) is that rule, because a
    billing period is itself unique per company and month.
    """

    billing_period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name='invoices')
    billing_run = models.ForeignKey(BillingRun, on_delete=models.PROTECT, related_name='invoices')
    freelancer = models.ForeignKey(Freelancer, on_delete=models.PROTECT, related_name='invoices')
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['billing_period', 'freelancer'], name='unique_invoice_per_month'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return self.number

    @property
    def number(self):
        return f'INV-{self.pk:06d}'

    @property
    def company(self):
        return self.billing_period.company

    @property
    def month(self):
        return self.billing_period.month


class InvoiceLine(models.Model):
    """One billed timesheet entry.

    Hours, rate and amount are copied from the entry and contract at billing
    time so the invoice never changes if those records are edited later.
    The one-to-one link means an entry can only ever be billed once.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='lines')
    timesheet_entry = models.OneToOneField(
        TimesheetEntry, on_delete=models.PROTECT, related_name='invoice_line'
    )
    date = models.DateField()
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    daily_rate = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ['date']


class InvoiceDocument(models.Model):
    """The issued invoice document, rendered once and stored as-is.

    The file is generated in the same transaction as the invoice and never
    regenerated, so what was sent is exactly what is on record even if the
    template changes later. Emails reference this row rather than carrying
    their own copy of the file. See billing/documents.py for the renderers.
    """

    FORMAT_TXT = 'txt'
    FORMAT_CHOICES = [
        (FORMAT_TXT, 'Plain text'),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='documents')
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    content = models.BinaryField(editable=False)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, help_text='Checksum of the content, to show it has not changed.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['invoice', 'format'], name='unique_invoice_document_format'),
        ]

    def __str__(self):
        return self.filename


class EmailDelivery(models.Model):
    """An outgoing billing email and the result of trying to send it.

    Rows are written in the same transaction as the invoice (or hold update)
    they belong to, and sent only after that transaction commits. A failed
    send leaves the row as failed so it can be retried, rather than rolling
    back the invoice. Emails that are out of date before they go out are
    cancelled rather than deleted, so there is a record of them.
    """

    KIND_INVOICE = 'invoice'
    KIND_READY_TO_REBILL = 'ready_to_rebill'
    KIND_CHOICES = [
        (KIND_INVOICE, 'Invoice'),
        (KIND_READY_TO_REBILL, 'Ready to re-bill'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]
    UNSENT_STATUSES = [STATUS_PENDING, STATUS_FAILED]

    billing_period = models.ForeignKey(BillingPeriod, on_delete=models.CASCADE, related_name='email_deliveries')
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, null=True, blank=True, related_name='email_deliveries'
    )
    attachment = models.ForeignKey(
        InvoiceDocument, on_delete=models.PROTECT, null=True, blank=True, related_name='email_deliveries'
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        verbose_name_plural = 'email deliveries'
