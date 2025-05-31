import asyncio
import json
import logging
from typing import Dict, Optional, Set

import requests
import websockets
from websockets.asyncio.server import serve, ServerConnection


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("bridge_server")


class OTreeBridge:
    """Bridge server that connects econagents WebSocket clients to oTree HTTP API."""

    def __init__(
        self, otree_url: str = "http://localhost:8000", rest_key: Optional[str] = None
    ):
        self.otree_url = otree_url
        self.rest_key = rest_key
        self.sessions: Dict[str, Dict] = {}
        self.participant_connections: Dict[str, ServerConnection] = {}
        self.participant_sessions: Dict[str, requests.Session] = {}
        self.session_participants: Dict[str, Set[str]] = {}

    async def handle_websocket(self, websocket: ServerConnection):
        """Handle incoming WebSocket connections from econagents clients."""
        participant_code = None
        try:
            logger.info("New WebSocket connection established")
            async for message in websocket:
                try:
                    data = json.loads(message)
                    logger.debug(f"Received message: {data}")
                    participant_code = await self.process_message(websocket, data)
                except json.JSONDecodeError:
                    logger.error("Invalid JSON received")
                    await self.send_error(websocket, "Invalid JSON")
                except Exception as e:
                    logger.exception(f"Error processing message: {e}")
                    await self.send_error(websocket, f"Error: {str(e)}")
        except websockets.exceptions.ConnectionClosed:
            logger.info(
                f"WebSocket connection closed for participant {participant_code}"
            )
        finally:
            # Clean up participant connection
            await self.cleanup_connection(websocket, participant_code)

    async def process_message(
        self, websocket: ServerConnection, data: dict
    ) -> Optional[str]:
        """Process messages from econagents clients."""
        msg_type = data.get("type")
        participant_code = None

        if msg_type == "join":
            participant_code = await self.handle_join(websocket, data)
        elif msg_type == "contribution":
            participant_code = data.get("participant_code")
            await self.handle_contribution(websocket, data)
        else:
            await self.send_error(websocket, f"Unknown message type: {msg_type}")

        return participant_code

    async def handle_join(self, websocket: ServerConnection, data: dict) -> str:
        """Handle participant joining."""
        try:
            participant_code = data.get("participant_code")
            participant_id = data.get("participant_id")

            self.participant_connections[participant_code] = websocket

            http_session = requests.Session()
            http_session.headers.update({"User-Agent": "oTree econagents bridge"})
            self.participant_sessions[participant_code] = http_session

            await self.initialize_participant(participant_code, participant_id)

            logger.info(f"Participant {participant_id} joined as {participant_code}")
            return participant_code

        except Exception as e:
            logger.exception(f"Error in handle_join: {e}")
            await self.send_error(websocket, f"Failed to join: {str(e)}")
            return ""

    async def handle_contribution(self, websocket: ServerConnection, data: dict):
        """Handle contribution submission to oTree."""
        participant_code = data.get("participant_code")
        participant_id = data.get("participant_id")
        contribution = data.get("contribution")

        if not all(
            [participant_code, participant_id is not None, contribution is not None]
        ):
            await self.send_error(websocket, "Missing required fields for contribution")
            return

        try:
            # Submit contribution to oTree
            success = await self.submit_contribution_to_otree(
                participant_code, contribution
            )

            if success:
                logger.info(
                    f"Contribution {contribution} submitted for participant {participant_code}"
                )
                # Wait for results and notify client
                await self.wait_for_results_and_notify(participant_code, participant_id)
            else:
                await self.send_error(websocket, "Failed to submit contribution")

        except Exception as e:
            logger.exception(f"Error handling contribution: {e}")
            await self.send_error(websocket, f"Error submitting contribution: {str(e)}")

    async def initialize_participant(self, participant_code: str, participant_id: int):
        """Initialize participant in oTree and navigate to the contribution page."""
        http_session = self.participant_sessions[participant_code]

        try:
            # Initialize participant
            init_url = f"{self.otree_url}/InitializeParticipant/{participant_code}"
            logger.info(f"Initializing participant {participant_code} at {init_url}")

            response = http_session.get(init_url, allow_redirects=False, timeout=10)
            response.raise_for_status()

            if not response.is_redirect:
                raise Exception(
                    f"Expected redirect from {init_url}, got {response.status_code}"
                )

            # Navigate to contribution page
            contrib_url = response.headers["Location"]
            if not contrib_url.startswith("http"):
                contrib_url = f"{self.otree_url}{contrib_url}"

            logger.info(
                f"Participant {participant_code} redirected to contribution page: {contrib_url}"
            )

            if not hasattr(self, "participant_urls"):
                self.participant_urls = {}
            self.participant_urls[participant_code] = contrib_url

            await self.send_game_state(participant_code, participant_id)

        except Exception as e:
            logger.exception(f"Error initializing participant {participant_code}: {e}")
            raise

    async def submit_contribution_to_otree(
        self, participant_code: str, contribution: int
    ) -> bool:
        """Submit contribution to oTree for the participant."""
        http_session = self.participant_sessions[participant_code]
        contrib_url = self.participant_urls.get(participant_code)

        if not contrib_url:
            logger.error(
                f"No contribution URL found for participant {participant_code}"
            )
            return False

        try:
            logger.info(
                f"Submitting contribution {contribution} for participant {participant_code}"
            )

            # Submit contribution
            payload = {"contribution": contribution}
            response = http_session.post(
                contrib_url, data=payload, allow_redirects=False, timeout=10
            )
            response.raise_for_status()

            if not response.is_redirect:
                logger.error(
                    f"Expected redirect after contribution submission, got {response.status_code}"
                )
                return False

            # Navigate to wait page
            wait_url = response.headers["Location"]
            if not wait_url.startswith("http"):
                wait_url = f"{self.otree_url}{wait_url}"

            logger.info(
                f"Participant {participant_code} redirected to wait page: {wait_url}"
            )
            self.participant_urls[participant_code] = wait_url

            return True

        except Exception as e:
            logger.exception(
                f"Error submitting contribution for {participant_code}: {e}"
            )
            return False

    async def wait_for_results_and_notify(
        self, participant_code: str, participant_id: int
    ):
        """Wait for oTree results page and notify the participant."""
        http_session = self.participant_sessions[participant_code]
        wait_url = self.participant_urls.get(participant_code)

        if not wait_url:
            logger.error(f"No wait URL found for participant {participant_code}")
            return

        try:
            # Poll the wait page until it redirects to results
            max_attempts = 10
            for attempt in range(max_attempts):
                logger.info(
                    f"Checking wait page for participant {participant_code} (attempt {attempt + 1})"
                )

                response = http_session.get(wait_url, allow_redirects=False, timeout=30)
                response.raise_for_status()

                if response.is_redirect:
                    # Wait page completed, redirect to results
                    results_url = response.headers["Location"]
                    if not results_url.startswith("http"):
                        results_url = f"{self.otree_url}{results_url}"

                    logger.info(
                        f"Participant {participant_code} redirected to results: {results_url}"
                    )

                    # Get results page
                    results_response = http_session.get(results_url, timeout=10)
                    results_response.raise_for_status()

                    # Parse results from the page (simplified - in real implementation you'd parse HTML)
                    # For now, send a mock results event
                    await self.send_results(participant_code, participant_id)

                    # Navigate through results page
                    results_post = http_session.post(
                        results_url, data={}, allow_redirects=False, timeout=10
                    )
                    if results_post.is_redirect:
                        final_url = results_post.headers["Location"]
                        if not final_url.startswith("http"):
                            final_url = f"{self.otree_url}{final_url}"
                        logger.info(
                            f"Participant {participant_code} completed experiment: {final_url}"
                        )

                        # Send game completion event
                        await self.send_game_completion(participant_code)

                    return

                elif "oTree-Wait-Page" in response.headers:
                    # Still on wait page, wait and retry
                    await asyncio.sleep(2)
                else:
                    logger.warning(
                        f"Unexpected response from wait page for {participant_code}"
                    )
                    break

            logger.warning(
                f"Wait page polling timed out for participant {participant_code}"
            )

        except Exception as e:
            logger.exception(f"Error waiting for results for {participant_code}: {e}")

    async def send_game_state(self, participant_code: str, participant_id: int):
        """Send initial game state to participant."""
        websocket = self.participant_connections.get(participant_code)
        if not websocket:
            return

        message = {
            "type": "event",
            "eventType": "round-started",
            "data": {
                "participant_id": participant_id,
                "endowment": 100,
                "num_players": 3,
                "round": 1,
            },
        }

        try:
            await websocket.send(json.dumps(message))
            logger.info(f"Sent game state to participant {participant_code}")
        except Exception as e:
            logger.error(f"Failed to send game state to {participant_code}: {e}")

    async def send_results(self, participant_code: str, participant_id: int):
        """Send results to participant."""
        websocket = self.participant_connections.get(participant_code)
        if not websocket:
            return

        # Mock results - in real implementation, parse from oTree results page
        message = {
            "type": "event",
            "eventType": "round-result",
            "data": {
                "participant_id": participant_id,
                "total_contribution": 150,  # Mock data
                "individual_share": 100.0,  # Mock data
                "your_contribution": 50,  # Mock data
                "final_payoff": 150,  # Mock data
            },
        }

        try:
            await websocket.send(json.dumps(message))
            logger.info(f"Sent results to participant {participant_code}")
        except Exception as e:
            logger.error(f"Failed to send results to {participant_code}: {e}")

    async def send_game_completion(self, participant_code: str):
        """Send game completion event."""
        websocket = self.participant_connections.get(participant_code)
        if not websocket:
            return

        message = {
            "type": "event",
            "eventType": "game-over",
            "data": {"message": "Experiment completed"},
        }

        try:
            await websocket.send(json.dumps(message))
            logger.info(f"Sent game completion to participant {participant_code}")
        except Exception as e:
            logger.error(f"Failed to send game completion to {participant_code}: {e}")

    async def send_error(self, websocket: ServerConnection, error_message: str):
        """Send error message to client."""
        message = {"type": "error", "message": error_message}
        try:
            await websocket.send(json.dumps(message))
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")

    async def cleanup_connection(
        self, websocket: ServerConnection, participant_code: Optional[str]
    ):
        """Clean up participant connection."""
        if participant_code:
            # Remove from active connections
            if participant_code in self.participant_connections:
                del self.participant_connections[participant_code]

            # Clean up HTTP session
            if participant_code in self.participant_sessions:
                self.participant_sessions[participant_code].close()
                del self.participant_sessions[participant_code]

            # Clean up URLs
            if (
                hasattr(self, "participant_urls")
                and participant_code in self.participant_urls
            ):
                del self.participant_urls[participant_code]

            logger.info(f"Cleaned up connection for participant {participant_code}")


async def main():
    """Start the bridge server."""
    # Configuration
    bridge_host = "localhost"
    bridge_port = 8765
    otree_url = "http://localhost:8000"
    rest_key = None

    logger.info(f"Starting oTree bridge server on {bridge_host}:{bridge_port}")
    logger.info(f"Connecting to oTree server at {otree_url}")

    bridge = OTreeBridge(otree_url=otree_url, rest_key=rest_key)

    async with serve(bridge.handle_websocket, bridge_host, bridge_port):
        logger.info("Bridge server running. Press Ctrl+C to stop.")
        try:
            await asyncio.Future()  # Run forever
        except KeyboardInterrupt:
            logger.info("Bridge server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
