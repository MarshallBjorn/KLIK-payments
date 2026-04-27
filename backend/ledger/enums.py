"""Enumy dla apki ledger."""

from django.db import models


class BeneficiaryType(models.TextChoices):
    BANK = 'BANK', 'Bank'
    KLIK = 'KLIK', 'KLIK'
    AGENT = 'AGENT', 'Agent'


class LedgerEntryType(models.TextChoices):
    MERCHANT_NET = 'MERCHANT_NET', 'Merchant net'
    KLIK_FEE_C2B = 'KLIK_FEE_C2B', 'KLIK fee (C2B)'
    AGENT_FEE = 'AGENT_FEE', 'Agent fee'
    BANK_TO_BANK = 'BANK_TO_BANK', 'Bank to bank (off-us merchant_net)'
    P2P_LOOKUP_FEE = 'P2P_LOOKUP_FEE', 'KLIK fee (P2P lookup)'


class SettlementSessionStatus(models.TextChoices):
    OPEN = 'OPEN', 'Open'
    NETTING = 'NETTING', 'Netting'
    SETTLING = 'SETTLING', 'Settling'
    COMPLETED = 'COMPLETED', 'Completed'
    FAILED = 'FAILED', 'Failed'


class SettlementTransferStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    DISPATCHED = 'DISPATCHED', 'Dispatched'
    COMPLETED = 'COMPLETED', 'Completed'
    FAILED = 'FAILED', 'Failed'
