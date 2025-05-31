"""
Utility functions for interacting with oTree REST API.
Shared between bridge server and game runner.
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def make_rest_api_call(
    otree_url: str, 
    rest_key: Optional[str], 
    method: str, 
    endpoint: str, 
    payload=None
):
    """Makes an authenticated REST API call to oTree."""
    headers = {"Content-Type": "application/json"}
    if rest_key:
        headers["otree-rest-key"] = rest_key
    
    url = f"{otree_url}{endpoint}"
    
    try:
        if method.upper() == "POST":
            response = requests.post(url, headers=headers, json=payload, timeout=10)
        elif method.upper() == "GET":
            response = requests.get(url, headers=headers, json=payload, timeout=10)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.json()
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error during REST API call to {url}: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"Response content: {e.response.text}")
        raise


def create_otree_session(
    otree_url: str = "http://localhost:8000",
    rest_key: Optional[str] = None,
    session_config_name: str = "public_goods_simple",
    num_participants: int = 3,
):
    """Creates an oTree session and returns session_code and participant_codes."""
    logger.info(
        f"Creating oTree session for config '{session_config_name}' with {num_participants} participants..."
    )
    
    payload = {
        "session_config_name": session_config_name,
        "num_participants": num_participants,
    }
    
    session_data = make_rest_api_call(otree_url, rest_key, "POST", "/api/sessions", payload)
    session_code = session_data["code"]
    logger.info(f"Session created with code: {session_code}")

    # Get participant codes
    session_details = make_rest_api_call(otree_url, rest_key, "POST", f"/api/get_session/{session_code}")
    participants_info = [
        {
            "code": p["code"],
            "id_in_session": p["id_in_session"],
            "label": p.get("label"),
        }
        for p in session_details.get("participants", [])
    ]
    
    logger.info(f"Participants in session: {participants_info}")
    return session_code, participants_info


def get_participant_configs(participants_info):
    """Convert oTree participant info to econagents participant configs."""
    return [
        {
            "participant_code": p["code"],
            "participant_id": p["id_in_session"],
        }
        for p in participants_info
    ] 
