import asyncio
import json
import logging
from typing import Dict, List, Optional, Set

from bs4 import BeautifulSoup
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
        self,
        rounds: List[str],
        otree_url: str = "http://localhost:8000",
    ):
        self.otree_url = otree_url
        self.sessions: Dict[str, Dict] = {}
        self.participant_connections: Dict[str, ServerConnection] = {}
        self.participant_sessions: Dict[str, requests.Session] = {}
        self.participant_urls: Dict[str, str] = {}
        self.session_participants: Dict[str, Set[str]] = {}
        self.form_fields: Dict[str, List[str]] = {}
        self.rounds = rounds

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
            await self.cleanup_connection(websocket, participant_code)

    async def process_message(
        self, websocket: ServerConnection, data: dict
    ) -> Optional[str]:
        """Process messages from econagents clients."""
        msg_type = data.get("type")
        participant_code = None

        if msg_type == "join":
            participant_code = await self.handle_join(websocket, data)
        else:
            participant_code = data.get("participant_code")
            await self.handle_task(websocket, data)

        return participant_code

    async def handle_join(self, websocket: ServerConnection, data: dict) -> str:
        """Handle participant joining using the participant_code provided by the client."""
        participant_code = data.get("participant_code")
        participant_id = data.get("participant_id")

        if not participant_code or participant_id is None:
            error_msg = "Participant code and ID are required for join message."
            logger.error(error_msg)
            await self.send_error(websocket, error_msg)
            return ""

        try:
            logger.info(
                f"Handling join for participant_code: {participant_code}, participant_id: {participant_id}"
            )

            self.participant_connections[participant_code] = websocket

            http_session = requests.Session()
            http_session.headers.update({"User-Agent": "oTree econagents bridge"})
            self.participant_sessions[participant_code] = http_session

            await self.initialize_participant(participant_code, participant_id)

            logger.info(
                f"Participant {participant_id} (code: {participant_code}) successfully joined and initialized."
            )
            return participant_code

        except Exception as e:
            logger.exception(
                f"Error in handle_join for participant_code {participant_code}: {e}"
            )
            await self.send_error(websocket, f"Error during join process: {str(e)}")
            if participant_code in self.participant_connections:
                del self.participant_connections[participant_code]
            if participant_code in self.participant_sessions:
                del self.participant_sessions[participant_code]
            return ""

    async def handle_task(self, websocket: ServerConnection, data: dict):
        """Handle task submission to oTree."""
        participant_code = data["participant_code"]
        participant_id = data["participant_id"]
        task_data = {}
        for f in self.form_fields.get(participant_code, []):
            task_data[f] = data.get(f)

        if not all(
            [participant_code, participant_id is not None, task_data is not None]
        ):
            await self.send_error(websocket, "Missing required fields for task")
            return

        try:
            success = await self.submit_task_to_otree(
                participant_code,  # type: ignore
                task_data,  # type: ignore
            )

            if success:
                logger.info(
                    f"Task {task_data} submitted for participant {participant_code}"
                )
                await self.wait_for_results_and_notify(
                    participant_code,
                    participant_id,  # type: ignore
                )
            else:
                await self.send_error(websocket, "Failed to submit contribution")

        except Exception as e:
            logger.exception(f"Error handling contribution: {e}")
            await self.send_error(websocket, f"Error submitting contribution: {str(e)}")

    def get_participant_configs(self, session_code: str) -> list:
        """Get participant configurations for econagents from the most recent or specified session."""
        session_info = self.sessions[session_code]
        return [
            {
                "participant_code": p["code"],
                "participant_id": p["id_in_session"],
            }
            for p in session_info["participants_info"]
        ]

    async def continue_to_next_page(
        self,
        http_session: requests.Session,
        participant_code: str,
        participant_id: int,
        url: str,
    ):
        """Continue to the next page for the participant."""
        response = await asyncio.to_thread(
            http_session.get, url, allow_redirects=False, timeout=10
        )
        response.raise_for_status()

        if not response.is_redirect:
            raise Exception(f"Expected redirect from {url}, got {response.status_code}")

        task_url = response.headers["Location"]

        if not task_url.startswith("http"):
            task_url = f"{self.otree_url}{task_url}"

        logger.info(
            f"Participant {participant_code} redirected to task page: {task_url}"
        )
        response_task = await asyncio.to_thread(
            http_session.get, task_url, allow_redirects=False, timeout=10
        )
        response_task.raise_for_status()

        if not hasattr(self, "participant_urls"):
            self.participant_urls = {}
        self.participant_urls[participant_code] = task_url

        if not hasattr(self, "form_fields"):
            self.form_fields = {}
        self.form_fields[participant_code] = []

        soup = BeautifulSoup(response_task.text, "html.parser")
        otree_data = soup.find("script", id="otree-data")

        for field in soup.select("._formfield input"):
            self.form_fields[participant_code].append(field.get("name"))  # type: ignore

        try:
            round_number = self.rounds.index(url.split("/")[-2]) + 1
        except ValueError:
            logger.error(f"Unknown round: {url.split('/')[-2]}")
            round_number = 0

        if otree_data:
            state_data = otree_data.text
            state_data = json.loads(state_data)
            state_data["round"] = round_number
            await self.send_game_state(participant_code, participant_id, state_data)

    async def initialize_participant(self, participant_code: str, participant_id: int):
        """Initialize participant in oTree and navigate to the contribution page."""
        http_session = self.participant_sessions.get(participant_code)
        if not http_session:
            logger.error(
                f"HTTP session not found for participant {participant_code} in initialize_participant."
            )
            raise ValueError(
                f"HTTP Session not found for participant {participant_code}"
            )

        try:
            init_url = f"{self.otree_url}/InitializeParticipant/{participant_code}"
            logger.info(f"Initializing participant {participant_code} at {init_url}")
            await self.continue_to_next_page(
                http_session,
                participant_code,
                participant_id,
                init_url,
            )
        except Exception as e:
            logger.exception(f"Error initializing participant {participant_code}: {e}")
            raise

    async def submit_task_to_otree(
        self, participant_code: str, task_data: dict
    ) -> bool:
        """Submit contribution to oTree for the participant."""
        http_session = self.participant_sessions[participant_code]
        contrib_url = self.participant_urls.get(participant_code)

        if not contrib_url:
            logger.error(
                f"HTTP session not found for participant {participant_code} in submit_contribution_to_otree."
            )
            return False

        try:
            logger.info(
                f"Submitting task {task_data} for participant {participant_code}"
            )

            response = await asyncio.to_thread(
                http_session.post,
                contrib_url,
                data=task_data,
                allow_redirects=False,
                timeout=10,
            )
            response.raise_for_status()

            if not response.is_redirect:
                logger.error(
                    f"Expected redirect after contribution submission, got {response.status_code}"
                )
                return False

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
        http_session = self.participant_sessions.get(participant_code)
        if not http_session:
            logger.error(
                f"HTTP session not found for participant {participant_code} in wait_for_results_and_notify."
            )
            return

        wait_url = self.participant_urls.get(participant_code)
        if not wait_url:
            logger.error(f"No wait URL found for participant {participant_code}")
            return

        try:
            max_attempts = 10
            for attempt in range(max_attempts):
                logger.info(
                    f"Checking wait page for participant {participant_code} (attempt {attempt + 1})"
                )

                response = await asyncio.to_thread(
                    http_session.get, wait_url, allow_redirects=False, timeout=30
                )
                response.raise_for_status()

                if response.is_redirect:
                    results_url = response.headers["Location"]
                    if not results_url.startswith("http"):
                        results_url = f"{self.otree_url}{results_url}"

                    logger.info(
                        f"Participant {participant_code} redirected to results: {results_url}"
                    )

                    results_response = await asyncio.to_thread(
                        http_session.get, results_url, timeout=10
                    )
                    results_response.raise_for_status()

                    soup = BeautifulSoup(results_response.text, "html.parser")
                    otree_data = soup.find("script", id="otree-data")

                    if otree_data:
                        state_data = json.loads(otree_data.text)
                        logger.info(f"Results data: {state_data}")

                    await self.send_game_state(
                        participant_code, participant_id, state_data
                    )

                    results_post = await asyncio.to_thread(
                        http_session.post,
                        results_url,
                        data={},
                        allow_redirects=False,
                        timeout=10,
                    )
                    if results_post.is_redirect:
                        final_url = results_post.headers["Location"]
                        if not final_url.startswith("http"):
                            final_url = f"{self.otree_url}{final_url}"
                        logger.info(
                            f"Participant {participant_code} completed experiment: {final_url}"
                        )
                        await self.send_game_completion(participant_code)

                    return

                elif "oTree-Wait-Page" in response.headers:
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

    async def send_game_state(
        self, participant_code: str, participant_id: int, state_data: dict
    ):
        """Send initial game state to participant."""
        websocket = self.participant_connections.get(participant_code)
        if not websocket:
            return

        message = {
            "type": "event",
            "eventType": "round-started",
            "data": {
                "participant_id": participant_id,
                **state_data,
            },
        }

        try:
            await websocket.send(json.dumps(message))
            logger.info(f"Sent game state to participant {participant_code}")
        except Exception as e:
            logger.error(f"Failed to send game state to {participant_code}: {e}")

    async def send_results(
        self, participant_code: str, participant_id: int, results_data: dict
    ):
        """Send results to participant."""
        websocket = self.participant_connections.get(participant_code)
        if not websocket:
            return

        message = {
            "type": "event",
            "eventType": "round-result",
            "data": results_data,
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
            if participant_code in self.participant_connections:
                del self.participant_connections[participant_code]

            if participant_code in self.participant_sessions:
                self.participant_sessions[participant_code].close()
                del self.participant_sessions[participant_code]

            if participant_code in self.participant_urls:
                del self.participant_urls[participant_code]

            logger.info(f"Cleaned up connection for participant {participant_code}")


async def main():
    """Start the bridge server."""
    # Configuration
    bridge_host = "localhost"
    bridge_port = 8765
    otree_url = "http://localhost:8000"

    logger.info(f"Starting oTree bridge server on {bridge_host}:{bridge_port}")
    logger.info(f"Connecting to oTree server at {otree_url}")

    bridge = OTreeBridge(
        otree_url=otree_url,
        rounds=["InitializeParticipant", "SubmitContribution", "Results"],
    )

    async with serve(bridge.handle_websocket, bridge_host, bridge_port):
        logger.info("Bridge server running. Press Ctrl+C to stop.")
        try:
            await asyncio.Future()  # Run forever
        except KeyboardInterrupt:
            logger.info("Bridge server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
