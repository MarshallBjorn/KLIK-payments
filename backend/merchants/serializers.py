from rest_framework import serializers

from merchants.models import Merchant


class MerchantListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ['id', 'name', 'zone', 'account_identifier']
