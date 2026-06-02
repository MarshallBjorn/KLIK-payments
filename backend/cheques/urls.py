"""URL routing dla apki cheques."""

from django.urls import path

from cheques.views import (
    ChequeCancelView,
    ChequeIssueView,
    ChequeRedeemView,
    ChequeStatusView,
)

urlpatterns = [
    path('cheques/issue', ChequeIssueView.as_view(), name='cheques-issue'),
    path('cheques/redeem', ChequeRedeemView.as_view(), name='cheques-redeem'),
    path('cheques/cancel', ChequeCancelView.as_view(), name='cheques-cancel'),
    path('cheques/status/<uuid:cheque_id>', ChequeStatusView.as_view(), name='cheques-status'),
]
