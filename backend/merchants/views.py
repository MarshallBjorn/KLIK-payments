from rest_framework.generics import ListAPIView

from agents.authentication import XKlikAgentApiKeyAuthentication
from merchants.models import Merchant
from merchants.serializers import MerchantListSerializer


class MerchantListView(ListAPIView):
    """GET /api/v1/merchants — lista aktywnych merchantów w strefie agenta."""

    authentication_classes = [XKlikAgentApiKeyAuthentication]
    serializer_class = MerchantListSerializer

    def get_queryset(self):
        agent = self.request.user
        return Merchant.objects.filter(active=True, zone=agent.zone).order_by('name')
