from django.db.models.signals import post_save
from django.dispatch import receiver

from contracts.models import TimesheetEntry

from .services import handle_entry_reviewed


@receiver(post_save, sender=TimesheetEntry)
def timesheet_entry_saved(sender, instance, **kwargs):
    handle_entry_reviewed(instance)
