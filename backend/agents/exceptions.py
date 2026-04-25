"""Custom exceptions dla apki agents."""


class NoActiveMSCAgreementError(Exception):
    """Agent nie ma aktywnie obowiązującej umowy MSC."""

    def __init__(self, agent_id, when=None):
        self.agent_id = agent_id
        self.when = when
        super().__init__(
            f'Agent {agent_id} nie ma aktywnej umowy MSC '
            f'{f"w chwili {when}" if when else "obecnie"}.'
        )
