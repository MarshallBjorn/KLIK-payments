"""Enumy dla apki codes."""

from django.db import models


class TransactionStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    AUTHORIZED = 'AUTHORIZED', 'Authorized'
    COMPLETED = 'COMPLETED', 'Completed'
    REJECTED = 'REJECTED', 'Rejected'
    TIMEOUT = 'TIMEOUT', 'Timeout'


class RejectReason(models.TextChoices):
    INSUFFICIENT_FUNDS = 'INSUFFICIENT_FUNDS', 'Insufficient funds'
    USER_DECLINED = 'USER_DECLINED', 'User declined'
    PIN_FAILED = 'PIN_FAILED', 'PIN authentication failed'
    AML_BLOCK = 'AML_BLOCK', 'AML compliance block'
    OTHER = 'OTHER', 'Other'


class CodeStatus(models.TextChoices):
    """Status kodu w Redisie. Nie pole modelu — używane w service."""

    ACTIVE = 'ACTIVE', 'Active'
    USED = 'USED', 'Used'
