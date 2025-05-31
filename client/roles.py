import json
import logging

from econagents import AgentRole
from econagents.core.events import Message
from econagents.core.manager.phase import TurnBasedPhaseManager
from econagents.llm import ChatOpenAI
from state import OTGameState

logger = logging.getLogger(__name__)


class PublicGoodsPlayer(AgentRole):
    """Agent role for public goods game players."""

    role = 1
    name = "Player"
    llm = ChatOpenAI(model_name="gpt-4o-mini")

    task_phases = [1]


class PublicGoodsManager(TurnBasedPhaseManager):
    """
    Manager for the Public Goods game.
    Manages interactions between the bridge server and agents.
    """

    def __init__(self, participant_code: str, participant_id: int):
        auth_kwargs = {
            "type": "join",
            "participant_code": participant_code,
            "participant_id": participant_id,
        }

        super().__init__(
            url="ws://localhost:8765",  # Bridge server URL
            auth_mechanism_kwargs=auth_kwargs,
            state=OTGameState(
                participant_id=participant_id,
                participant_code=participant_code,
            ),
            agent_role=PublicGoodsPlayer(),
            phase_transition_event="round-started",
            phase_identifier_key="round",
        )
        self.participant_code = participant_code
        self.participant_id = participant_id
