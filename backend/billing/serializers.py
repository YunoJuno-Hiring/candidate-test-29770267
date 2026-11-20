from rest_framework import serializers

from accounts.serializers import CompanySerializer, FreelancerSerializer

from .models import BillingPeriod, EmailDelivery, Invoice, InvoiceDocument, InvoiceLine
from .services import is_billable_month, parse_month


class MonthField(serializers.Field):
    """A calendar month as 'YYYY-MM', held internally as the first day of that month."""

    default_error_messages = {'invalid': 'Enter a month in the format YYYY-MM.'}

    def to_internal_value(self, data):
        month = parse_month(data)
        if month is None:
            self.fail('invalid')
        return month

    def to_representation(self, value):
        return value.strftime('%Y-%m')


class BillingRunRequestSerializer(serializers.Serializer):
    month = MonthField()

    def validate_month(self, value):
        if not is_billable_month(value):
            raise serializers.ValidationError(
                'Billing can only be run for months that have finished.'
            )
        return value


class BillingPeriodSerializer(serializers.ModelSerializer):
    month = MonthField()
    held_freelancers = serializers.SerializerMethodField()

    class Meta:
        model = BillingPeriod
        fields = ['month', 'status', 'completed_at', 'held_freelancers']

    def get_held_freelancers(self, obj):
        freelancers = [hold.freelancer for hold in obj.holds.select_related('freelancer')]
        return FreelancerSerializer(freelancers, many=True).data


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ['timesheet_entry', 'date', 'hours', 'daily_rate', 'amount']


class InvoiceDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceDocument
        fields = ['id', 'format', 'filename', 'content_type', 'size', 'created_at']


class EmailDeliverySerializer(serializers.ModelSerializer):
    attachment = serializers.SlugRelatedField(slug_field='filename', read_only=True)

    class Meta:
        model = EmailDelivery
        fields = ['recipient', 'attachment', 'status', 'attempts', 'last_error', 'sent_at']


class InvoiceSerializer(serializers.ModelSerializer):
    month = MonthField(read_only=True)
    company = CompanySerializer(read_only=True)
    freelancer = FreelancerSerializer(read_only=True)

    class Meta:
        model = Invoice
        fields = ['id', 'number', 'month', 'company', 'freelancer', 'total', 'created_at']


class InvoiceDetailSerializer(InvoiceSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    documents = InvoiceDocumentSerializer(many=True, read_only=True)
    email_deliveries = EmailDeliverySerializer(many=True, read_only=True)

    class Meta(InvoiceSerializer.Meta):
        fields = InvoiceSerializer.Meta.fields + ['lines', 'documents', 'email_deliveries']
