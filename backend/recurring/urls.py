"""URL routing dla apki recurring."""

from django.urls import path

from recurring.views import (
    RecurringCancelView,
    RecurringCreateView,
    RecurringDetailView,
    RecurringExecutionsView,
    RecurringListView,
    RecurringPauseView,
    RecurringResumeView,
)

urlpatterns = [
    path('recurring/create', RecurringCreateView.as_view(), name='recurring-create'),
    path('recurring', RecurringListView.as_view(), name='recurring-list'),
    path(
        'recurring/<uuid:recurring_transfer_id>',
        RecurringDetailView.as_view(),
        name='recurring-detail',
    ),
    path(
        'recurring/<uuid:recurring_transfer_id>/pause',
        RecurringPauseView.as_view(),
        name='recurring-pause',
    ),
    path(
        'recurring/<uuid:recurring_transfer_id>/resume',
        RecurringResumeView.as_view(),
        name='recurring-resume',
    ),
    path(
        'recurring/<uuid:recurring_transfer_id>/cancel',
        RecurringCancelView.as_view(),
        name='recurring-cancel',
    ),
    path(
        'recurring/<uuid:recurring_transfer_id>/executions',
        RecurringExecutionsView.as_view(),
        name='recurring-executions',
    ),
]
