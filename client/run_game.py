import asyncio
import logging
from pathlib import Path

from econagents.core.game_runner import GameRunner, TurnBasedGameRunnerConfig
from roles import PublicGoodsManager
from state import OTGameState
from otree_utils import create_otree_session, get_participant_configs

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("public_goods_runner")


async def main():
    """Run the oTree public goods game with econagents."""

    logger.info("Starting oTree Public Goods Game with econagents")

    session_code, participants_info = create_otree_session()
    participant_configs = get_participant_configs(participants_info)
    logger.info(
        f"Successfully created oTree session {session_code} with {len(participant_configs)} participants"
    )

    config = TurnBasedGameRunnerConfig(
        game_id=1,
        hostname="localhost",
        port=8765,
        path="",
        logs_dir=Path(__file__).parent / "logs",
        prompts_dir=Path(__file__).parent / "prompts",
        state_class=OTGameState,
        phase_transition_event="round-started",
        phase_identifier_key="round",
        max_game_duration=300,
        log_level=logging.DEBUG,
        observability_provider="langsmith",
    )

    agents = [
        PublicGoodsManager(
            participant_code=config["participant_code"],
            participant_id=config["participant_id"],
        )
        for config in participant_configs
    ]

    logger.info(f"Starting game with {len(agents)} agents")
    for agent in agents:
        logger.info(f"Agent: {agent.participant_code}")

    # logger.info(
    #     f"Human players URL: http://localhost:8000/InitializeParticipant/{participant_configs[2]['participant_code']}"
    # )
    # input("Enter to continue...")

    runner = GameRunner(config=config, agents=agents)
    await runner.run_game()

    logger.info("Game completed")


if __name__ == "__main__":
    asyncio.run(main())
