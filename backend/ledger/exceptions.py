"""Wyjątki domenowe apki ledger."""


class LedgerError(Exception):
    """Base class dla błędów ledger."""


class TransactionNotCompletedError(LedgerError):
    """Próba record_c2b_transaction na transakcji nie w statusie COMPLETED."""

    def __init__(self, transaction_id, status):
        self.transaction_id = transaction_id
        self.status = status
        super().__init__(f'Transakcja {transaction_id} ma status {status}, oczekiwano COMPLETED.')


class FeesNotCalculatedError(LedgerError):
    """Próba record_c2b_transaction na transakcji bez policzonych fees."""

    def __init__(self, transaction_id):
        self.transaction_id = transaction_id
        super().__init__(
            f'Transakcja {transaction_id} nie ma policzonych fees '
            f'(klik_fee/agent_fee/merchant_net).'
        )


class ActiveSessionExistsError(LedgerError):
    """Próba create_session gdy aktywna sesja już istnieje dla strefy."""

    def __init__(self, zone, existing_session_id):
        self.zone = zone
        self.existing_session_id = existing_session_id
        super().__init__(f'Aktywna sesja dla strefy {zone} już istnieje: {existing_session_id}.')


class InvalidSessionStateError(LedgerError):
    """Próba operacji na sesji w niewłaściwym statusie."""

    def __init__(self, session_id, current_status, expected_statuses):
        self.session_id = session_id
        self.current_status = current_status
        self.expected_statuses = expected_statuses
        super().__init__(
            f'Sesja {session_id} ma status {current_status}, '
            f'oczekiwano jeden z: {expected_statuses}.'
        )
