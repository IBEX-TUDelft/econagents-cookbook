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
        otree_url: str = "http://localhost:8000",
    ):
        self.otree_url = otree_url
        self.sessions: Dict[str, Dict] = {}
        self.participant_connections: Dict[str, ServerConnection] = {}
        self.participant_sessions: Dict[str, requests.Session] = {}
        self.participant_urls: Dict[str, str] = {}
        self.session_participants: Dict[str, Set[str]] = {}
        self.form_fields: Dict[str, List[str]] = {}
        self.participant_phases: Dict[
            str, int
        ] = {}  # Track current phase number per participant

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

            # Initialize phase counter
            self.participant_phases[participant_code] = 0

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
                # Continue navigating through pages
                await self.navigate_experiment(
                    participant_code,
                    participant_id,  # type: ignore
                )
            else:
                await self.send_error(websocket, "Failed to submit task")

        except Exception as e:
            logger.exception(f"Error handling task: {e}")
            await self.send_error(websocket, f"Error submitting task: {str(e)}")

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

        logger.info(f"Participant {participant_code} redirected to page: {task_url}")
        response_task = await asyncio.to_thread(
            http_session.get, task_url, allow_redirects=False, timeout=10
        )
        response_task.raise_for_status()

        self.participant_urls[participant_code] = task_url

        # Clear previous form fields
        self.form_fields[participant_code] = []

        soup = BeautifulSoup(response_task.text, "html.parser")
        otree_data = soup.find("script", id="otree-data")

        # Extract form fields
        for field in soup.select("._formfield input"):
            field_name = field.get("name")
            if field_name:
                self.form_fields[participant_code].append(str(field_name))

        # If there are form fields, this is an action page
        if self.form_fields[participant_code] and otree_data:
            # Increment phase number
            self.participant_phases[participant_code] += 1
            phase_number = self.participant_phases[participant_code]

            state_data = json.loads(otree_data.text)
            state_data["phase"] = phase_number
            state_data["required_fields"] = self.form_fields[participant_code]
            await self.send_phase_update(participant_code, participant_id, state_data)

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

    async def navigate_experiment(self, participant_code: str, participant_id: int):
        """Navigate through experiment pages, handling both wait pages and action pages."""
        http_session = self.participant_sessions.get(participant_code)
        if not http_session:
            logger.error(
                f"HTTP session not found for participant {participant_code} in navigate_experiment."
            )
            return

        current_url = self.participant_urls.get(participant_code)
        if not current_url:
            logger.error(f"No current URL found for participant {participant_code}")
            return

        try:
            max_attempts = 30  # Increased for potentially longer experiments
            for attempt in range(max_attempts):
                logger.info(
                    f"Checking page for participant {participant_code} (attempt {attempt + 1})"
                )

                response = await asyncio.to_thread(
                    http_session.get, current_url, allow_redirects=False, timeout=30
                )
                response.raise_for_status()

                # Handle redirects
                if response.is_redirect:
                    next_url = response.headers["Location"]
                    if not next_url.startswith("http"):
                        next_url = f"{self.otree_url}{next_url}"

                    logger.info(
                        f"Participant {participant_code} redirected to: {next_url}"
                    )

                    # Get the next page
                    next_response = await asyncio.to_thread(
                        http_session.get, next_url, timeout=10
                    )
                    next_response.raise_for_status()

                    self.participant_urls[participant_code] = next_url

                    # Parse the page
                    soup = BeautifulSoup(next_response.text, "html.parser")
                    otree_data = soup.find("script", id="otree-data")

                    # Check for form fields
                    self.form_fields[participant_code] = []
                    for field in soup.select("._formfield input"):
                        field_name = field.get("name")
                        if field_name:
                            self.form_fields[participant_code].append(str(field_name))

                    # If there are form fields, this is an action page
                    if self.form_fields[participant_code] and otree_data:
                        # Increment phase number
                        self.participant_phases[participant_code] += 1
                        phase_number = self.participant_phases[participant_code]

                        state_data = json.loads(otree_data.text)
                        state_data["phase"] = phase_number
                        state_data["required_fields"] = self.form_fields[
                            participant_code
                        ]
                        await self.send_phase_update(
                            participant_code, participant_id, state_data
                        )
                        return  # Exit and wait for next action from econagents

                    # If no form fields but has state data, might be results or info page
                    elif otree_data:
                        state_data = json.loads(otree_data.text)
                        logger.info(f"Page data: {state_data}")

                        # For results pages, send results event
                        await self.send_results(
                            participant_code, participant_id, state_data
                        )

                        # Try to continue by posting empty form
                        continue_response = await asyncio.to_thread(
                            http_session.post,
                            next_url,
                            data={},
                            allow_redirects=False,
                            timeout=10,
                        )

                        if continue_response.is_redirect:
                            redirect_url = continue_response.headers.get("Location")
                            if redirect_url:
                                if not redirect_url.startswith("http"):
                                    redirect_url = f"{self.otree_url}{redirect_url}"
                                
                                # Check if experiment is complete
                                if "OutOfRangeNotification" in redirect_url:
                                    logger.info(
                                        f"Participant {participant_code} completed experiment"
                                    )
                                    await self.send_game_completion(participant_code)
                                    return

                                # Fetch and parse the redirected page
                                redirect_response = await asyncio.to_thread(
                                    http_session.get, redirect_url, timeout=10
                                )
                                redirect_response.raise_for_status()

                                self.participant_urls[participant_code] = redirect_url

                                # Parse the redirected page
                                redirect_soup = BeautifulSoup(redirect_response.text, "html.parser")
                                redirect_otree_data = redirect_soup.find("script", id="otree-data")

                                # Check for form fields in the redirected page
                                self.form_fields[participant_code] = []
                                for field in redirect_soup.select("._formfield input"):
                                    field_name = field.get("name")
                                    if field_name:
                                        self.form_fields[participant_code].append(str(field_name))

                                # If the redirected page has form fields, it's a new task page
                                if self.form_fields[participant_code] and redirect_otree_data:
                                    # Increment phase number
                                    self.participant_phases[participant_code] += 1
                                    phase_number = self.participant_phases[participant_code]

                                    redirect_state_data = json.loads(redirect_otree_data.text)
                                    redirect_state_data["phase"] = phase_number
                                    redirect_state_data["required_fields"] = self.form_fields[
                                        participant_code
                                    ]
                                    await self.send_phase_update(
                                        participant_code, participant_id, redirect_state_data
                                    )
                                    return  # Exit and wait for next action from econagents

                                # If not a task page, update current_url and continue navigating
                                current_url = redirect_url

                            continue  # Keep navigating
                        else:
                            # No redirect, might be stuck
                            logger.warning(f"No redirect from {next_url}")
                            return

                # Handle wait pages
                elif "oTree-Wait-Page" in response.headers:
                    logger.info(f"Participant {participant_code} on wait page")
                    await asyncio.sleep(2)
                    continue

                else:
                    logger.warning(f"Unexpected response type for {participant_code}")
                    break

            logger.warning(f"Navigation timed out for participant {participant_code}")

        except Exception as e:
            logger.exception(f"Error navigating experiment for {participant_code}: {e}")

    async def send_phase_update(
        self, participant_code: str, participant_id: int, state_data: dict
    ):
        """Send phase update to participant when entering a new action page."""
        websocket = self.participant_connections.get(participant_code)
        if not websocket:
            return

        message = {
            "type": "event",
            "eventType": "phase-transition",
            "data": {
                "participant_id": participant_id,
                **state_data,
            },
        }

        try:
            await websocket.send(json.dumps(message))
            logger.info(
                f"Sent phase update to participant {participant_code}: phase={state_data.get('phase')}"
            )
        except Exception as e:
            logger.error(f"Failed to send phase update to {participant_code}: {e}")

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

            if participant_code in self.participant_phases:
                del self.participant_phases[participant_code]

            if participant_code in self.form_fields:
                del self.form_fields[participant_code]

            logger.info(f"Cleaned up connection for participant {participant_code}")


async def main():
    """Start the bridge server."""
    # Configuration
    bridge_host = "localhost"
    bridge_port = 8765
    otree_url = "http://localhost:8000"

    logger.info(f"Starting oTree bridge server on {bridge_host}:{bridge_port}")
    logger.info(f"Connecting to oTree server at {otree_url}")

    bridge = OTreeBridge(otree_url=otree_url)

    async with serve(bridge.handle_websocket, bridge_host, bridge_port):
        logger.info("Bridge server running. Press Ctrl+C to stop.")
        try:
            await asyncio.Future()  # Run forever
        except KeyboardInterrupt:
            logger.info("Bridge server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
