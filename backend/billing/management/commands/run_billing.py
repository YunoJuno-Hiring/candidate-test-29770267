import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from billing.services import (
    is_billable_month,
    month_start,
    parse_month,
    run_billing_for_all_companies,
)


class Command(BaseCommand):
    help = 'Run monthly billing for every company. Defaults to last month.'

    def add_arguments(self, parser):
        parser.add_argument('--month', help='Month to bill, as YYYY-MM.')

    def handle(self, *args, **options):
        if options['month']:
            month = parse_month(options['month'])
            if month is None:
                raise CommandError('--month must be in the format YYYY-MM.')
        else:
            month = month_start(month_start(timezone.localdate()) - datetime.timedelta(days=1))

        if not is_billable_month(month):
            raise CommandError('Billing can only be run for months that have finished.')

        for result in run_billing_for_all_companies(month):
            period = result.period
            self.stdout.write(
                f'{period.company.name} {month:%Y-%m}: {len(result.invoices)} invoice(s), '
                f'{len(result.held_freelancers)} on hold, {result.emails_failed} email(s) failed, '
                f'period {period.status}'
            )
