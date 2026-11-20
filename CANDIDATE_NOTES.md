## What I built and why

I chose Task B because, whilst my experience over the past 30 years covers both frontend and backend, I was particularly drawn to the challenges around data modelling, idempotency and transaction boundaries. It gave me the opportunity to demonstrate my problem-solving approach and my understanding of the problem space itself — freelancer timesheets and invoicing — along with the technical considerations involved in designing a reliable, production-ready implementation.

## Key implementation notes

1. **One invoice per freelancer, company and month — and holding back incomplete freelancers.** The brief asked for three endpoints; I added a fourth, which follows from one key design decision. The stub comment in `billing/models.py` stated that there should only ever be one invoice per company, freelancer and month. As a contractor who submits monthly timesheets myself, I'm aware of the complications when that isn't enforced. It's enforced in the database by a unique constraint on `Invoice` (`unique_invoice_per_month`, `backend/billing/models.py:84`) together with one `BillingPeriod` per company and month (`models.py:30`).

   The consequence is that a freelancer can't be invoiced while any of their hours for the month are still `draft` or `submitted` — those hours would otherwise never be billable. So a billing run invoices every freelancer whose entries are all approved or rejected, and *holds* anyone with unreviewed hours (`BLOCKING_STATUSES` and the loop in `run_billing`, `backend/billing/services.py:27` and `:113–120`). Once a freelancer is invoiced, their entries for that month are locked against changes (409 Conflict, `backend/contracts/views.py:128` and `:161`). The extra endpoint, `GET /api/billing/periods/<YYYY-MM>/` (`backend/billing/views.py:63`), reports the month's status (`not_run`, `incomplete` or `complete`) and which freelancers are held, so the Billing page can show it.

2. **Only finished months can be billed.** Billing can't run for the current month or a future one, because more hours could still be logged against it. Restricting billing to completed months keeps every invoice final and avoids invoicing a partial month and topping it up later. The rule is enforced by the API (`validate_month` in `backend/billing/serializers.py:27`, using `is_billable_month` in `services.py:57`); the Billing page also stops you picking those months and disables the button (`frontend/src/pages/Billing.tsx:79` and `:90`).

3. **Admins are told when a held freelancer is ready.** When the last unreviewed entry for a held freelancer is approved or rejected, the company's admins are emailed that billing can be re-run for that month (a `post_save` signal on `TimesheetEntry`, `backend/billing/signals.py:9`, calling `handle_entry_reviewed` in `services.py:197`). The email is queued with `transaction.on_commit` (`services.py:242`), so a rolled-back approval never triggers it, and it's sent only once per hold. Re-running billing skips freelancers who already have an invoice for that month (`services.py:114`), in line with point 1, and cancels any of these notifications that haven't gone out yet, since the run is the action they were asking for (`services.py:105`).

4. **Idempotency.** The rules are enforced by the database, not just by application code: one invoice per period and freelancer, and a one-to-one link between `InvoiceLine` and `TimesheetEntry`, so an entry can never be billed twice. A billing run can safely be repeated: it only bills what's still outstanding and recalculates the period's status from current data each time. Emails use an outbox pattern: each one is saved as an `EmailDelivery` row first and sent afterwards by `send_pending_emails()`, and retries only pick up `pending` or `failed` rows.

5. **Transaction boundaries.** A billing run is one transaction (`transaction.atomic()` in `run_billing`): invoices, lines, the stored invoice document, holds and the queued email rows are all saved together, or none are. Emails are sent only after that transaction commits, so a failed send can't roll back an invoice — the row is marked `failed` with the error and retried later. `select_for_update()` on the billing period means two simultaneous runs for the same company and month queue up behind each other rather than racing. Each email is sent under its own row lock (`select_for_update(skip_locked=True)`), so two retry processes can't send the same email.


## My workflow

I started by understanding the problem space, which was made easier by many years as a contractor using various live payroll and timesheet systems. From the brief I listed the main considerations, which became the key parts of the implementation above. I used Claude Code to generate the code; this was a straightforward and mostly error-free process, with some fine-tuning and prompting needed in places but not excessively. I specified the automated tests I wanted, and also tested manually through the frontend. That uncovered a bug where, after a "ready to be billed" email had failed to send while the mail server was down, the next billing run retried it alongside the two invoice emails — sending three emails instead of two, one of which was out of date. I also manually picked up on some UTF-8 encoding issues in the attached text invoices which were fixed. 

## Next steps

Given four more hours, I would add PDF invoices. Invoice emails already carry the invoice as an attachment (currently a `.txt` file stored as an `InvoiceDocument`, with a short covering note in the email body). With PDFs, the design stays the same, I'd just add a PDF renderer and an HTML/CSS template in `billing/documents.py`, using a library such as WeasyPrint, and switch the configured format. I would also look at making the invoices legally compliant to include things like tax and vat details, etc.

I would also integrate Task A. Approvals made there feed straight into the hold and notification logic above.

There is little observability in the application which is more of a production-readiness task so I would start taking a look at what metrics we can start recording and also what approaches we can implement to expose these.

Beyond that, I'd look at sending emails from a background worker with scheduled retries, and at integrating invoices with a payment processor, though that would most likely exceed four hours.
