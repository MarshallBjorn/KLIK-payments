"""Custom exceptions dla CodeService."""


class CodeGenerationFailedError(Exception):
    """Nie udało się wygenerować unikalnego kodu po N próbach."""

    def __init__(self, attempts: int):
        self.attempts = attempts
        super().__init__(
            f'Nie udało się wygenerować unikalnego kodu po {attempts} próbach. '
            f'Pula kodów może być nasycona — alert dla operatora.'
        )


class CodeAlreadyUsedError(Exception):
    """Próba użycia kodu który już jest USED."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Kod {code} został już użyty.')


class CodeNotFoundError(Exception):
    """Kod nie istnieje w Redis (wygasł lub nigdy nie istniał)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Kod {code} nie istnieje (wygasł lub niepoprawny).')
