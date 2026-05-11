from django.urls import path

from merchants.views import MerchantListView

urlpatterns = [
    path('merchants', MerchantListView.as_view(), name='merchants-list'),
]
