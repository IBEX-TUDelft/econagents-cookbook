from pydantic import Field

from econagents.core.state.fields import EventField
from econagents.core.state.game import (
    GameState,
    MetaInformation,
    PrivateInformation,
    PublicInformation,
)


class OTMeta(MetaInformation):
    """Meta information for the oTree public goods game."""

    participant_id: int = EventField(default=0, exclude_from_mapping=True)
    participant_code: str = EventField(default="", exclude_from_mapping=True)
    phase: int = EventField(default=0)


class OTPrivate(PrivateInformation):
    """Private information for the oTree public goods game."""

    endowment: int = EventField(default=100)
    contribution_made: int = EventField(default=0, event_key="your_contribution")


class OTPublic(PublicInformation):
    """Public information for the oTree public goods game."""

    num_players: int = EventField(default=3)
    total_contribution: int = EventField(
        default=0, event_key="group_total_contribution"
    )
    individual_share: float = EventField(default=0.0)


class OTGameState(GameState):
    """Game state for the oTree game."""

    meta: OTMeta = Field(default_factory=OTMeta)
    private_information: OTPrivate = Field(default_factory=OTPrivate)
    public_information: OTPublic = Field(default_factory=OTPublic)

    def __init__(self, participant_id: int = 0, participant_code: str = "", **kwargs):
        super().__init__(**kwargs)
        self.meta.participant_id = participant_id
        self.meta.participant_code = participant_code
