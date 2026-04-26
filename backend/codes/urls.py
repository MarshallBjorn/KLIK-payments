"""URL routing dla apki codes."""

from django.urls import path

from codes.views import (
    CodeGenerateView,
    PaymentInitiateView,
    PaymentStatusView,
)

urlpatterns = [
    path('codes/generate', CodeGenerateView.as_view(), name='codes-generate'),
    path('payments/initiate', PaymentInitiateView.as_view(), name='payments-initiate'),
    path(
        'payments/status/<uuid:transaction_id>',
        PaymentStatusView.as_view(),
        name='payments-status',
    ),
]
