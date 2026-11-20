from django.contrib import admin

from .models import (
    BillingHold,
    BillingPeriod,
    BillingRun,
    EmailDelivery,
    Invoice,
    InvoiceDocument,
    InvoiceLine,
)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    readonly_fields = ['timesheet_entry', 'date', 'hours', 'daily_rate', 'amount']
    can_delete = False


class InvoiceDocumentInline(admin.TabularInline):
    model = InvoiceDocument
    extra = 0
    fields = ['format', 'filename', 'content_type', 'size', 'sha256', 'created_at']
    readonly_fields = fields
    can_delete = False


class EmailDeliveryInline(admin.TabularInline):
    model = EmailDelivery
    extra = 0
    fields = ['kind', 'recipient', 'attachment', 'status', 'attempts', 'last_error', 'sent_at']
    readonly_fields = fields
    can_delete = False


class BillingHoldInline(admin.TabularInline):
    model = BillingHold
    extra = 0
    readonly_fields = ['freelancer', 'created_at', 'notified_at']
    can_delete = False


@admin.register(BillingPeriod)
class BillingPeriodAdmin(admin.ModelAdmin):
    list_display = ['company', 'month', 'status', 'completed_at']
    list_filter = ['status', 'company']
    inlines = [BillingHoldInline]


@admin.register(BillingRun)
class BillingRunAdmin(admin.ModelAdmin):
    list_display = ['billing_period', 'triggered_by', 'invoices_generated', 'created_at']


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['number', 'billing_period', 'freelancer', 'total', 'created_at']
    list_filter = ['billing_period__company']
    inlines = [InvoiceLineInline, InvoiceDocumentInline, EmailDeliveryInline]


@admin.register(EmailDelivery)
class EmailDeliveryAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'kind', 'status', 'attempts', 'created_at', 'sent_at']
    list_filter = ['status', 'kind']
