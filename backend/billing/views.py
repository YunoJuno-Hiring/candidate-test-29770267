from django.db.models import Prefetch
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BillingPeriod, EmailDelivery, Invoice, InvoiceDocument
from .serializers import (
    BillingPeriodSerializer,
    BillingRunRequestSerializer,
    InvoiceDetailSerializer,
    InvoiceSerializer,
)
from .services import parse_month, run_billing


# Reported for months with no BillingPeriod yet. Not stored on the model,
# because a period only exists once billing has been run for it.
BILLING_PERIOD_NOT_RUN = 'not_run'


def _admin_company(user):
    if hasattr(user, 'company_admin'):
        return user.company_admin.company
    return None


def _invoices_visible_to(user):
    """Admins see their company's invoices; freelancers see their own."""
    qs = Invoice.objects.select_related('billing_period__company', 'freelancer')
    company = _admin_company(user)
    if company:
        return qs.filter(billing_period__company=company)
    if hasattr(user, 'freelancer'):
        return qs.filter(freelancer=user.freelancer)
    return qs.none()


class BillingRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = _admin_company(request.user)
        if company is None:
            return Response(
                {'detail': 'Only company admins can run billing.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = BillingRunRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = run_billing(company, serializer.validated_data['month'], triggered_by=request.user)
        return Response({
            'run_id': result.run.id,
            'status': 'completed',
            'invoices_generated': len(result.invoices),
            'emails_failed': result.emails_failed,
            'period': BillingPeriodSerializer(result.period).data,
        })


class BillingPeriodDetailView(APIView):
    """The billing status of a month for the admin's company.

    A month that billing hasn't been run for yet is a normal state, not a
    missing resource, so it returns 200 with status 'not_run' rather than 404.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, month):
        company = _admin_company(request.user)
        if company is None:
            return Response(
                {'detail': 'Only company admins can view billing periods.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        month_start = parse_month(month)
        if month_start is None:
            return Response(
                {'month': 'Enter a month in the format YYYY-MM.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        period = BillingPeriod.objects.filter(company=company, month=month_start).first()
        if period is None:
            return Response({
                'month': month,
                'status': BILLING_PERIOD_NOT_RUN,
                'completed_at': None,
                'held_freelancers': [],
            })
        return Response(BillingPeriodSerializer(period).data)


class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _invoices_visible_to(request.user)

        month_param = request.query_params.get('month')
        if month_param:
            month = parse_month(month_param)
            if month is None:
                return Response(
                    {'month': 'Enter a month in the format YYYY-MM.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(billing_period__month=month)

        return Response(InvoiceSerializer(qs, many=True).data)


class InvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        qs = _invoices_visible_to(request.user).prefetch_related(
            'lines',
            Prefetch('documents', queryset=InvoiceDocument.objects.defer('content')),
            Prefetch('email_deliveries', queryset=EmailDelivery.objects.select_related('attachment').defer('attachment__content')),
        )
        try:
            invoice = qs.get(pk=pk)
        except Invoice.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceDetailSerializer(invoice).data)
