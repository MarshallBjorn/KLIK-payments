"""Service layer dla apki agents — kalkulacja splitu, lookup MSC."""

from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from agents.exceptions import NoActiveMSCAgreementError
from agents.models import Agent, MSCAgreement


class AgentService:
    """Logika biznesowa związana z agentami i ich umowami MSC."""

    @staticmethod
    def get_active_msc(agent: Agent, when=None) -> MSCAgreement:
        """
        Zwraca aktywnie obowiązującą umowę MSC dla agenta.

        Raises:
            NoActiveMSCAgreement: jeśli agent nie ma aktywnej umowy.
        """

        when = when or timezone.now()
        active = (
            MSCAgreement.objects.filter(
                agent=agent,
                valid_from__lte=when,
            )
            .filter(
                models_or_q(when),
            )
            .order_by('-valid_from')
            .first()
        )

        if active is None:
            raise NoActiveMSCAgreementError(agent.id, when)

        return active

    @staticmethod
    def calculate_split(agent: Agent, amount_gross: Decimal, when=None) -> dict:
        """
        Kalkulacja splitu prowizji dla danego agenta i kwoty brutto.

        Returns:
            dict z kluczami: klik_fee, agent_fee, merchant_net (wszystkie Decimal).
        """
        msc = AgentService.get_active_msc(agent, when=when)

        klik_fee = _round_money(amount_gross * msc.klik_fee_perc / Decimal('100'))
        agent_fee = _round_money(amount_gross * msc.agent_fee_perc / Decimal('100'))
        merchant_net = amount_gross - klik_fee - agent_fee

        return {
            'klik_fee': klik_fee,
            'agent_fee': agent_fee,
            'merchant_net': merchant_net,
            'msc_id': msc.id,
        }


def _round_money(value: Decimal) -> Decimal:
    """Zaokrąglenie do 2 miejsc po przecinku, half-up (banker-rounding)."""
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def models_or_q(when):
    """Helper: valid_to IS NULL OR valid_to > when."""
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gt=when)
