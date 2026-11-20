import datetime
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import Company
from contracts.models import TimesheetEntry

from .documents import create_invoice_document
from .models import (
    BillingHold,
    BillingPeriod,
    BillingRun,
    EmailDelivery,
    Invoice,
    InvoiceLine,
)

logger = logging.getLogger(__name__)

BLOCKING_STATUSES = [TimesheetEntry.STATUS_DRAFT, TimesheetEntry.STATUS_SUBMITTED]


@dataclass
class BillingRunResult:
    run: BillingRun
    period: BillingPeriod
    invoices: list = field(default_factory=list)
    held_freelancers: list = field(default_factory=list)
    emails_failed: int = 0


def parse_month(value):
    """Parse 'YYYY-MM' into the first day of that month, or return None if invalid."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
        return None
    try:
        return datetime.datetime.strptime(value, '%Y-%m').date()
    except ValueError:
        return None


def month_start(day):
    return day.replace(day=1)


def next_month_start(month):
    return (month + datetime.timedelta(days=32)).replace(day=1)


def is_billable_month(month):
    """Only months that have fully ended can be billed."""
    return month < month_start(timezone.localdate())


def entries_for_month(company, month):
    return TimesheetEntry.objects.filter(
        contract__company=company,
        date__gte=month,
        date__lt=next_month_start(month),
    )


def is_invoiced(contract, day):
    """Whether this freelancer has already been invoiced for the month containing `day`.

    Once they have, their timesheet entries for that month are locked.
    """
    return Invoice.objects.filter(
        billing_period__company_id=contract.company_id,
        billing_period__month=month_start(day),
        freelancer_id=contract.freelancer_id,
    ).exists()


def run_billing(company, month, triggered_by=None):
    """Invoice every freelancer at `company` whose hours for `month` are all reviewed.

    Safe to run any number of times. Freelancers already invoiced for the month
    are skipped, freelancers with draft or submitted hours are put on hold, and
    the period is complete once nobody is on hold.
    """
    period, _ = BillingPeriod.objects.get_or_create(company=company, month=month)

    with transaction.atomic():
        # Lock the period so two runs for the same company and month queue up
        # behind each other instead of both trying to create the same invoices.
        period = BillingPeriod.objects.select_for_update().get(pk=period.pk)
        run = BillingRun.objects.create(billing_period=period, triggered_by=triggered_by)
        result = BillingRunResult(run=run, period=period)

        # "Ready to be billed" emails ask an admin to run billing for this
        # month. Now that it is running, any that haven't gone out yet are
        # out of date: each freelancer they mention is about to be invoiced
        # or held again, and a new email follows when they next clear.
        period.email_deliveries.filter(
            kind=EmailDelivery.KIND_READY_TO_REBILL,
            status__in=EmailDelivery.UNSENT_STATUSES,
        ).update(status=EmailDelivery.STATUS_CANCELLED)

        already_invoiced = set(period.invoices.values_list('freelancer_id', flat=True))
        entries_by_freelancer = defaultdict(list)
        entries = entries_for_month(company, month).select_related('contract__freelancer__user')
        for entry in entries.order_by('date'):
            entries_by_freelancer[entry.contract.freelancer].append(entry)

        for freelancer, freelancer_entries in entries_by_freelancer.items():
            if freelancer.id in already_invoiced:
                continue

            if any(e.status in BLOCKING_STATUSES for e in freelancer_entries):
                BillingHold.objects.get_or_create(billing_period=period, freelancer=freelancer)
                result.held_freelancers.append(freelancer)
                continue

            approved = [e for e in freelancer_entries if e.status == TimesheetEntry.STATUS_APPROVED]
            if approved:
                result.invoices.append(_create_invoice(run, freelancer, approved))

        # Only the freelancers held by this run stay on hold; everyone else
        # has either been invoiced now or has nothing left to bill.
        period.holds.exclude(freelancer__in=result.held_freelancers).delete()

        if result.held_freelancers:
            period.status = BillingPeriod.STATUS_INCOMPLETE
            period.completed_at = None
        elif period.status != BillingPeriod.STATUS_COMPLETE:
            period.status = BillingPeriod.STATUS_COMPLETE
            period.completed_at = timezone.now()
        period.save(update_fields=['status', 'completed_at'])

        run.invoices_generated = len(result.invoices)
        run.save(update_fields=['invoices_generated'])

    # Emails go out only after the invoices are committed. Invoice emails for
    # this period that failed on an earlier run are retried here too.
    result.emails_failed = send_pending_emails(
        period.email_deliveries.filter(kind=EmailDelivery.KIND_INVOICE)
    )
    return result


def _create_invoice(run, freelancer, entries):
    period = run.billing_period
    invoice = Invoice.objects.create(
        billing_period=period, billing_run=run, freelancer=freelancer, total=0
    )
    lines = [
        InvoiceLine(
            invoice=invoice,
            timesheet_entry=entry,
            date=entry.date,
            hours=entry.hours,
            daily_rate=entry.contract.daily_rate,
            amount=entry.contract.cost_for_hours(entry.hours),
        )
        for entry in entries
    ]
    InvoiceLine.objects.bulk_create(lines)
    invoice.total = sum(line.amount for line in lines)
    invoice.save(update_fields=['total'])

    # The invoice itself goes in an attached document; the email body is
    # just a covering note pointing at it.
    document = create_invoice_document(invoice)
    subject = f'Invoice {invoice.number} for {period.month:%B %Y}'
    body = render_to_string('billing/email/invoice_cover.txt', {
        'invoice': invoice,
        'document': document,
        'company': period.company,
        'freelancer': freelancer,
        'month': period.month,
    })
    recipients = [period.company.billing_email, freelancer.user.email]
    EmailDelivery.objects.bulk_create([
        EmailDelivery(
            billing_period=period,
            invoice=invoice,
            attachment=document,
            kind=EmailDelivery.KIND_INVOICE,
            recipient=recipient,
            subject=subject,
            body=body,
        )
        for recipient in recipients
        if recipient
    ])
    return invoice


def handle_entry_reviewed(entry):
    """Tell company admins when a held freelancer is ready to be re-billed.

    Called whenever a timesheet entry is saved. If the freelancer is on hold
    for that month and none of their hours are draft or submitted any more,
    admins get one email saying the month can be billed again.
    """
    contract = entry.contract
    month = month_start(entry.date)

    with transaction.atomic():
        hold = (
            BillingHold.objects.select_for_update()
            .select_related('billing_period__company', 'freelancer')
            .filter(
                billing_period__company_id=contract.company_id,
                billing_period__month=month,
                freelancer_id=contract.freelancer_id,
            )
            .first()
        )
        if hold is None:
            return

        still_blocked = entries_for_month(hold.billing_period.company, month).filter(
            contract__freelancer_id=contract.freelancer_id,
            status__in=BLOCKING_STATUSES,
        ).exists()

        if still_blocked:
            if hold.notified_at:
                hold.notified_at = None
                hold.save(update_fields=['notified_at'])
            return

        if hold.notified_at:
            return

        hold.notified_at = timezone.now()
        hold.save(update_fields=['notified_at'])
        delivery_ids = [d.pk for d in _queue_ready_to_rebill_emails(hold)]

        # on_commit rather than sending straight away: if the caller wraps
        # several approvals in one transaction that later rolls back, no
        # email goes out for a change that never happened.
        transaction.on_commit(
            lambda: send_pending_emails(EmailDelivery.objects.filter(pk__in=delivery_ids))
        )


def _queue_ready_to_rebill_emails(hold):
    period = hold.billing_period
    subject = f'{hold.freelancer.name} is ready to be billed for {period.month:%B %Y}'
    body = render_to_string('billing/email/ready_to_rebill.txt', {
        'company': period.company,
        'freelancer': hold.freelancer,
        'month': period.month,
    })
    admin_emails = period.company.admins.select_related('user').values_list('user__email', flat=True)
    return EmailDelivery.objects.bulk_create([
        EmailDelivery(
            billing_period=period,
            kind=EmailDelivery.KIND_READY_TO_REBILL,
            recipient=email,
            subject=subject,
            body=body,
        )
        for email in admin_emails
        if email
    ])


def send_pending_emails(deliveries):
    """Send any pending or failed deliveries in `deliveries`. Returns how many failed.

    Each row is locked while it is sent, so two processes retrying at the same
    time won't both send it. Delivery is at-least-once: if the process dies
    after the email is sent but before the row is marked sent, it will be sent
    again on the next retry.
    """
    pending_ids = deliveries.filter(
        status__in=EmailDelivery.UNSENT_STATUSES
    ).values_list('pk', flat=True)

    failed = 0
    for pk in list(pending_ids):
        with transaction.atomic():
            delivery = (
                EmailDelivery.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status__in=EmailDelivery.UNSENT_STATUSES)
                .first()
            )
            if delivery is None:
                continue

            delivery.attempts += 1
            try:
                message = EmailMessage(delivery.subject, delivery.body, to=[delivery.recipient])
                if delivery.attachment_id:
                    document = delivery.attachment
                    message.attach(document.filename, bytes(document.content), document.content_type)
                message.send()
            except Exception as exc:
                logger.exception('Failed to send billing email %s', delivery.pk)
                delivery.status = EmailDelivery.STATUS_FAILED
                delivery.last_error = str(exc)
                failed += 1
            else:
                delivery.status = EmailDelivery.STATUS_SENT
                delivery.sent_at = timezone.now()
                delivery.last_error = ''
            delivery.save(update_fields=['status', 'attempts', 'last_error', 'sent_at'])
    return failed


def run_billing_for_all_companies(month):
    """Run billing for every company that has timesheet entries in `month`."""
    companies = Company.objects.filter(
        contracts__timesheet_entries__date__gte=month,
        contracts__timesheet_entries__date__lt=next_month_start(month),
    ).distinct()
    return [run_billing(company, month) for company in companies]
