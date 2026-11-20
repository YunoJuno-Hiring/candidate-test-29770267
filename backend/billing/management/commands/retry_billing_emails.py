from django.core.management.base import BaseCommand

from billing.models import EmailDelivery
from billing.services import send_pending_emails


class Command(BaseCommand):
    help = 'Retry every billing email that has not been sent yet.'

    def handle(self, *args, **options):
        unsent = EmailDelivery.objects.filter(status__in=EmailDelivery.UNSENT_STATUSES)
        total = unsent.count()
        failed = send_pending_emails(unsent)
        self.stdout.write(f'Retried {total} unsent billing email(s), {failed} still failing.')
