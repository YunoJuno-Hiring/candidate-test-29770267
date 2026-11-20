import datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone

from accounts.models import CompanyAdmin, Freelancer
from billing import documents
from billing.documents import RenderedDocument
from billing.models import BillingHold, BillingPeriod, EmailDelivery, Invoice, InvoiceDocument
from billing.services import run_billing
from contracts.models import Contract, TimesheetEntry

MARCH = datetime.date(2026, 3, 1)


def make_entry(contract, day, status='approved', hours='8.0'):
    return TimesheetEntry.objects.create(
        contract=contract, date=datetime.date(2026, 3, day), hours=Decimal(hours), status=status
    )


@pytest.fixture
def second_contract(northstar):
    user = User.objects.create_user(username='sam@test.test', email='sam@test.test', password='x')
    freelancer = Freelancer.objects.create(user=user, name='Sam')
    return Contract.objects.create(
        company=northstar,
        freelancer=freelancer,
        daily_rate=Decimal('400.00'),
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 12, 31),
    )


# --- Pricing ---

def test_cost_for_hours_prices_against_an_eight_hour_day():
    contract = Contract(daily_rate=Decimal('600.00'))
    assert contract.cost_for_hours(Decimal('8')) == Decimal('600.00')
    assert contract.cost_for_hours(Decimal('7.5')) == Decimal('562.50')
    assert contract.cost_for_hours(Decimal('1')) == Decimal('75.00')


# --- Billing run ---

@pytest.mark.django_db
def test_run_invoices_approved_hours_for_the_month(admin_client, active_contract, mailoutbox):
    make_entry(active_contract, 2)
    make_entry(active_contract, 3, hours='7.5')
    make_entry(active_contract, 4, status='rejected')
    TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 4, 1), hours=Decimal('8'), status='approved'
    )

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-03'}, format='json')

    assert resp.status_code == 200
    assert resp.data['invoices_generated'] == 1
    assert resp.data['period']['status'] == 'complete'

    invoice = Invoice.objects.get()
    assert invoice.total == Decimal('1162.50')
    assert [line.date.day for line in invoice.lines.all()] == [2, 3]
    assert {m.to[0] for m in mailoutbox} == {'billing@northstar.test', 'freelancer@test.test'}
    assert invoice.number in mailoutbox[0].subject


@pytest.mark.django_db
def test_invoice_lines_keep_the_rate_at_billing_time(northstar, active_contract):
    make_entry(active_contract, 2)
    run_billing(northstar, MARCH)

    active_contract.daily_rate = Decimal('900.00')
    active_contract.save()

    line = Invoice.objects.get().lines.get()
    assert line.daily_rate == Decimal('600.00')
    assert line.amount == Decimal('600.00')


@pytest.mark.django_db
def test_running_twice_does_not_duplicate_invoices_or_emails(northstar, active_contract, mailoutbox):
    make_entry(active_contract, 2)

    first = run_billing(northstar, MARCH)
    second = run_billing(northstar, MARCH)

    assert len(first.invoices) == 1
    assert len(second.invoices) == 0
    assert Invoice.objects.count() == 1
    assert len(mailoutbox) == 2


@pytest.mark.django_db
@pytest.mark.parametrize('blocking_status', ['submitted', 'draft'])
def test_unreviewed_hours_hold_back_only_that_freelancer(
    northstar, active_contract, second_contract, blocking_status
):
    make_entry(active_contract, 2)
    make_entry(active_contract, 3, status=blocking_status)
    make_entry(second_contract, 2)

    result = run_billing(northstar, MARCH)

    assert [inv.freelancer for inv in result.invoices] == [second_contract.freelancer]
    assert result.held_freelancers == [active_contract.freelancer]
    assert result.period.status == BillingPeriod.STATUS_INCOMPLETE
    assert BillingHold.objects.filter(freelancer=active_contract.freelancer).exists()


@pytest.mark.django_db
def test_rerun_after_review_invoices_held_freelancer_and_completes_period(
    northstar, active_contract, second_contract
):
    make_entry(active_contract, 2)
    pending = make_entry(active_contract, 3, status='submitted')
    make_entry(second_contract, 2)
    run_billing(northstar, MARCH)

    pending.status = 'approved'
    pending.save()
    result = run_billing(northstar, MARCH)

    assert [inv.freelancer for inv in result.invoices] == [active_contract.freelancer]
    assert result.invoices[0].lines.count() == 2
    assert result.period.status == BillingPeriod.STATUS_COMPLETE
    assert result.period.completed_at is not None
    assert not BillingHold.objects.exists()
    assert Invoice.objects.count() == 2


@pytest.mark.django_db
def test_freelancer_with_only_rejected_hours_gets_no_invoice(northstar, active_contract):
    make_entry(active_contract, 2, status='rejected')

    result = run_billing(northstar, MARCH)

    assert result.invoices == []
    assert result.period.status == BillingPeriod.STATUS_COMPLETE


@pytest.mark.django_db
def test_run_only_bills_the_admins_company(admin_client, active_contract, other_contract):
    make_entry(active_contract, 2)
    make_entry(other_contract, 2)

    admin_client.post('/api/billing/runs/', {'month': '2026-03'}, format='json')

    assert Invoice.objects.get().company == active_contract.company


@pytest.mark.django_db
def test_management_command_bills_every_company(active_contract, other_contract):
    make_entry(active_contract, 2)
    make_entry(other_contract, 2)

    call_command('run_billing', '--month', '2026-03')

    assert Invoice.objects.count() == 2


# --- Run validation and permissions ---

@pytest.mark.django_db
@pytest.mark.parametrize('month', ['2026-3', 'March', '', None])
def test_run_rejects_malformed_month(admin_client, month):
    resp = admin_client.post('/api/billing/runs/', {'month': month}, format='json')
    assert resp.status_code == 400


@pytest.mark.django_db
def test_run_rejects_month_that_has_not_finished(admin_client):
    this_month = timezone.localdate().strftime('%Y-%m')
    resp = admin_client.post('/api/billing/runs/', {'month': this_month}, format='json')
    assert resp.status_code == 400
    assert 'finished' in str(resp.data['month'])


@pytest.mark.django_db
def test_freelancer_cannot_run_billing(freelancer_client):
    resp = freelancer_client.post('/api/billing/runs/', {'month': '2026-03'}, format='json')
    assert resp.status_code == 403


# --- Email delivery ---

@pytest.mark.django_db
def test_email_failure_keeps_invoice_and_is_retried_on_next_run(northstar, active_contract, mailoutbox):
    make_entry(active_contract, 2)

    with mock.patch('billing.services.EmailMessage.send', side_effect=ConnectionRefusedError('SMTP down')):
        result = run_billing(northstar, MARCH)

    assert result.emails_failed == 2
    assert Invoice.objects.count() == 1
    assert set(EmailDelivery.objects.values_list('status', flat=True)) == {'failed'}
    assert EmailDelivery.objects.first().last_error == 'SMTP down'

    retry = run_billing(northstar, MARCH)

    assert retry.emails_failed == 0
    assert len(mailoutbox) == 2
    assert all(d.status == 'sent' and d.attempts == 2 for d in EmailDelivery.objects.all())


# --- Invoice documents ---

@pytest.mark.django_db
def test_invoice_is_attached_as_a_text_document_with_a_covering_note(northstar, active_contract, mailoutbox):
    make_entry(active_contract, 2)
    make_entry(active_contract, 3, hours='4.0')

    invoice = run_billing(northstar, MARCH).invoices[0]

    document = InvoiceDocument.objects.get()
    assert document.invoice == invoice
    assert document.filename == f'{invoice.number}.txt'
    assert document.content_type == 'text/plain'
    raw = bytes(document.content)
    assert raw.startswith(b'\xef\xbb\xbf'), 'UTF-8 BOM so viewers do not misread the £ sign'
    text = raw.decode('utf-8')
    assert '2026-03-02' in text and '2026-03-03' in text
    assert 'Total: £900.00' in text

    for message in mailoutbox:
        assert 'Please find attached invoice' in message.body
        assert '2026-03-02' not in message.body
        [(filename, content, mimetype)] = message.attachments
        assert filename == document.filename
        assert mimetype == 'text/plain'
        assert content == text


@pytest.mark.django_db
def test_both_invoice_emails_reference_one_stored_document(northstar, active_contract):
    make_entry(active_contract, 2)

    run_billing(northstar, MARCH)

    document = InvoiceDocument.objects.get()
    deliveries = EmailDelivery.objects.filter(kind=EmailDelivery.KIND_INVOICE)
    assert deliveries.count() == 2
    assert all(d.attachment_id == document.id for d in deliveries)


@pytest.mark.django_db
def test_retried_email_attaches_the_stored_document(northstar, active_contract, mailoutbox):
    make_entry(active_contract, 2)
    with mock.patch('billing.services.EmailMessage.send', side_effect=ConnectionRefusedError('SMTP down')):
        run_billing(northstar, MARCH)

    run_billing(northstar, MARCH)

    assert InvoiceDocument.objects.count() == 1
    assert [len(m.attachments) for m in mailoutbox] == [1, 1]


@pytest.mark.django_db
def test_configured_document_format_changes_only_the_attachment(
    northstar, active_contract, mailoutbox, settings
):
    def render_fake_pdf(invoice):
        return RenderedDocument(content=b'%PDF-1.7 fake', content_type='application/pdf', extension='pdf')

    settings.BILLING_INVOICE_DOCUMENT_FORMAT = 'pdf'
    make_entry(active_contract, 2)

    with mock.patch.dict(documents.RENDERERS, {'pdf': render_fake_pdf}):
        invoice = run_billing(northstar, MARCH).invoices[0]

    assert InvoiceDocument.objects.get().format == 'pdf'
    [(filename, content, mimetype)] = mailoutbox[0].attachments
    assert filename == f'{invoice.number}.pdf'
    assert mimetype == 'application/pdf'
    assert content == b'%PDF-1.7 fake'


def test_unknown_document_format_is_an_error():
    with pytest.raises(ValueError, match='No invoice renderer'):
        documents.create_invoice_document(Invoice(), format='docx')


# --- Ready-to-rebill notifications ---

@pytest.fixture
def held_freelancer(northstar, active_contract, admin_user):
    make_entry(active_contract, 2)
    first = make_entry(active_contract, 3, status='submitted')
    second = make_entry(active_contract, 4, status='submitted')
    run_billing(northstar, MARCH)
    return first, second


def ready_to_rebill_emails(mailoutbox):
    return [m for m in mailoutbox if 'ready to be billed' in m.subject]


@pytest.mark.django_db
def test_admins_notified_once_when_last_held_entry_is_reviewed(
    admin_client, held_freelancer, mailoutbox, django_capture_on_commit_callbacks
):
    first, second = held_freelancer

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.patch(f'/api/timesheets/{first.id}/', {'status': 'approved'}, format='json')
    assert ready_to_rebill_emails(mailoutbox) == []

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.patch(
            f'/api/timesheets/{second.id}/',
            {'status': 'rejected', 'rejection_reason': 'Not worked'},
            format='json',
        )
    emails = ready_to_rebill_emails(mailoutbox)
    assert len(emails) == 1
    assert emails[0].to == ['admin@test.test']
    assert 'March 2026' in emails[0].subject

    with django_capture_on_commit_callbacks(execute=True):
        second.save()
    assert len(ready_to_rebill_emails(mailoutbox)) == 1


@pytest.mark.django_db
def test_admins_notified_again_if_freelancer_is_blocked_again(
    held_freelancer, mailoutbox, django_capture_on_commit_callbacks
):
    first, second = held_freelancer

    with django_capture_on_commit_callbacks(execute=True):
        for entry in (first, second):
            entry.status = 'approved'
            entry.save()
        late = make_entry(first.contract, 5, status='submitted')
        late.status = 'approved'
        late.save()

    assert len(ready_to_rebill_emails(mailoutbox)) == 2


@pytest.mark.django_db
def test_other_admins_of_the_company_are_notified_too(
    northstar, held_freelancer, mailoutbox, django_capture_on_commit_callbacks
):
    other = User.objects.create_user(username='a2@test.test', email='a2@test.test', password='x')
    CompanyAdmin.objects.create(user=other, company=northstar)

    with django_capture_on_commit_callbacks(execute=True):
        for entry in held_freelancer:
            entry.status = 'approved'
            entry.save()

    assert {m.to[0] for m in ready_to_rebill_emails(mailoutbox)} == {'admin@test.test', 'a2@test.test'}


@pytest.mark.django_db
def test_unsent_ready_to_rebill_email_is_cancelled_once_billing_runs(
    northstar, held_freelancer, mailoutbox, django_capture_on_commit_callbacks
):
    smtp_down = mock.patch(
        'billing.services.EmailMessage.send', side_effect=ConnectionRefusedError('SMTP down')
    )

    with smtp_down, django_capture_on_commit_callbacks(execute=True):
        for entry in held_freelancer:
            entry.status = 'approved'
            entry.save()
    notification = EmailDelivery.objects.get(kind=EmailDelivery.KIND_READY_TO_REBILL)
    assert notification.status == EmailDelivery.STATUS_FAILED

    with smtp_down:
        result = run_billing(northstar, MARCH)

    assert result.emails_failed == 2
    notification.refresh_from_db()
    assert notification.status == EmailDelivery.STATUS_CANCELLED

    run_billing(northstar, MARCH)

    assert ready_to_rebill_emails(mailoutbox) == []
    assert notification.attempts == 1


@pytest.mark.django_db
def test_retry_command_sends_failed_ready_to_rebill_email(
    held_freelancer, mailoutbox, django_capture_on_commit_callbacks
):
    with mock.patch('billing.services.EmailMessage.send', side_effect=ConnectionRefusedError('SMTP down')):
        with django_capture_on_commit_callbacks(execute=True):
            for entry in held_freelancer:
                entry.status = 'approved'
                entry.save()

    call_command('retry_billing_emails')

    assert len(ready_to_rebill_emails(mailoutbox)) == 1
    assert EmailDelivery.objects.get(kind=EmailDelivery.KIND_READY_TO_REBILL).status == 'sent'


# --- Locking invoiced months ---

@pytest.mark.django_db
def test_entries_in_an_invoiced_month_cannot_be_changed(admin_client, northstar, active_contract):
    entry = make_entry(active_contract, 2)
    run_billing(northstar, MARCH)

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'rejected', 'rejection_reason': 'x'}, format='json')

    assert resp.status_code == 409
    entry.refresh_from_db()
    assert entry.status == 'approved'


@pytest.mark.django_db
def test_entries_cannot_be_moved_into_an_invoiced_month(freelancer_client, northstar, active_contract):
    make_entry(active_contract, 2)
    run_billing(northstar, MARCH)
    april = TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 4, 1), hours=Decimal('8'), status='draft'
    )

    resp = freelancer_client.patch(f'/api/timesheets/{april.id}/', {'date': '2026-03-10'}, format='json')

    assert resp.status_code == 409


@pytest.mark.django_db
def test_hours_cannot_be_logged_in_an_invoiced_month(freelancer_client, northstar, active_contract):
    make_entry(active_contract, 2)
    run_billing(northstar, MARCH)

    resp = freelancer_client.post('/api/timesheets/', {
        'contract': active_contract.id, 'date': '2026-03-20', 'hours': '8.0',
    })

    assert resp.status_code == 409


@pytest.mark.django_db
def test_held_freelancer_can_still_have_entries_reviewed(admin_client, held_freelancer):
    first, _ = held_freelancer
    resp = admin_client.patch(f'/api/timesheets/{first.id}/', {'status': 'approved'}, format='json')
    assert resp.status_code == 200


# --- Reading invoices and periods ---

@pytest.mark.django_db
def test_admin_lists_only_their_company_invoices_for_the_month(
    admin_client, meridian, northstar, active_contract, other_contract
):
    make_entry(active_contract, 2)
    make_entry(other_contract, 2)
    run_billing(northstar, MARCH)
    run_billing(meridian, MARCH)

    resp = admin_client.get('/api/invoices/?month=2026-03')

    assert resp.status_code == 200
    assert len(resp.data) == 1
    assert resp.data[0]['company']['id'] == northstar.id
    assert resp.data[0]['month'] == '2026-03'
    assert admin_client.get('/api/invoices/?month=2026-02').data == []


@pytest.mark.django_db
def test_invoice_list_rejects_malformed_month(admin_client):
    assert admin_client.get('/api/invoices/?month=03-2026').status_code == 400


@pytest.mark.django_db
def test_invoice_detail_includes_lines_and_deliveries(admin_client, northstar, active_contract):
    make_entry(active_contract, 2)
    invoice = run_billing(northstar, MARCH).invoices[0]

    resp = admin_client.get(f'/api/invoices/{invoice.id}/')

    assert resp.status_code == 200
    assert resp.data['number'] == invoice.number
    assert resp.data['total'] == '600.00'
    assert len(resp.data['lines']) == 1
    assert [d['filename'] for d in resp.data['documents']] == [f'{invoice.number}.txt']
    assert 'content' not in resp.data['documents'][0]
    assert {d['status'] for d in resp.data['email_deliveries']} == {'sent'}
    assert {d['attachment'] for d in resp.data['email_deliveries']} == {f'{invoice.number}.txt'}


@pytest.mark.django_db
def test_freelancer_sees_own_invoices_across_companies(
    freelancer_client, northstar, meridian, active_contract, other_contract, second_contract
):
    make_entry(active_contract, 2)
    make_entry(other_contract, 2)
    make_entry(second_contract, 2)
    run_billing(northstar, MARCH)
    run_billing(meridian, MARCH)

    resp = freelancer_client.get('/api/invoices/')

    assert len(resp.data) == 2
    assert {inv['freelancer']['name'] for inv in resp.data} == {'Test Freelancer'}


@pytest.mark.django_db
def test_invoice_detail_is_404_for_another_company(admin_client, meridian, other_contract):
    make_entry(other_contract, 2)
    invoice = run_billing(meridian, MARCH).invoices[0]

    assert admin_client.get(f'/api/invoices/{invoice.id}/').status_code == 404


@pytest.mark.django_db
def test_billing_period_shows_held_freelancers(admin_client, held_freelancer):
    resp = admin_client.get('/api/billing/periods/2026-03/')

    assert resp.status_code == 200
    assert resp.data['status'] == 'incomplete'
    assert [f['name'] for f in resp.data['held_freelancers']] == ['Test Freelancer']


@pytest.mark.django_db
def test_billing_period_is_not_run_before_billing_has_run(admin_client):
    resp = admin_client.get('/api/billing/periods/2026-03/')

    assert resp.status_code == 200
    assert resp.data == {
        'month': '2026-03', 'status': 'not_run', 'completed_at': None, 'held_freelancers': [],
    }


@pytest.mark.django_db
def test_billing_period_rejects_malformed_month(admin_client):
    assert admin_client.get('/api/billing/periods/March/').status_code == 400


@pytest.mark.django_db
def test_freelancer_cannot_view_billing_periods(freelancer_client):
    assert freelancer_client.get('/api/billing/periods/2026-03/').status_code == 403
