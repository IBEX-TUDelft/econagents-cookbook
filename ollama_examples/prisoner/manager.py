from typing import Any

from dotenv import load_dotenv
from econagents import AgentRole
from econagents.core.manager.phase import TurnBasedPhaseManager
from econagents.llm import ChatOllama

load_dotenv()


class Prisoner(AgentRole):
    """Base class for prisoner agents in the Prisoner's Dilemma game."""

    role = 1
    name = "Prisoner"
    llm = ChatOllama(model_name="qwen2.5:7b")


class PDManager(TurnBasedPhaseManager):
    """
    Manager for the Prisoner's Dilemma game.
    Manages interactions between the server and agents.
    """

    def __init__(self, game_id: int, auth_mechanism_kwargs: dict[str, Any]):
        super().__init__(
            auth_mechanism_kwargs=auth_mechanism_kwargs,
            agent_role=Prisoner(),
        )
        self.game_id = game_id
