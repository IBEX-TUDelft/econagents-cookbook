import threading
import time
from typing import Optional

import requests

OTREE_SERVER_URL = "http://localhost:8000"
OTREE_REST_KEY: Optional[str] = None
SESSION_CONFIG_NAME = "public_goods_simple"
NUM_PARTICIPANTS = 2


def make_rest_api_call(method, endpoint, payload=None):
    """Makes an authenticated REST API call to oTree."""
    headers = {"otree-rest-key": OTREE_REST_KEY, "Content-Type": "application/json"}
    url = f"{OTREE_SERVER_URL}{endpoint}"
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
        print(f"Error during REST API call to {url}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Response content: {e.response.text}")
        raise


def create_otree_session(config_name, num_participants):
    """Creates an oTree session and returns session_code and participant_codes."""
    print(
        f"Creating oTree session for config '{config_name}' with {num_participants} participants..."
    )
    payload = {
        "session_config_name": config_name,
        "num_participants": num_participants,
    }
    session_data = make_rest_api_call("POST", "/api/sessions", payload)
    session_code = session_data["code"]
    print(f"Session created with code: {session_code}")

    # Get participant codes
    session_details = make_rest_api_call("POST", f"/api/get_session/{session_code}")
    participants_info = [
        {
            "code": p["code"],
            "id_in_session": p["id_in_session"],
            "label": p.get("label"),
        }
        for p in session_details.get("participants", [])
    ]
    print(f"Participants in session: {participants_info}")
    return session_code, participants_info


def run_experiment_for_participant(participant_info):
    """Simulates a single participant going through the experiment."""
    participant_code = participant_info["code"]
    participant_id = participant_info["id_in_session"]
    print(
        f"\n--- Simulating Participant {participant_id} (Code: {participant_code}) ---"
    )

    with requests.Session() as http_session:
        http_session.headers.update({"User-Agent": "oTree econagents client"})

        current_url = f"{OTREE_SERVER_URL}/InitializeParticipant/{participant_code}"
        print(f"GET: {current_url}")
        try:
            response = http_session.get(current_url, allow_redirects=False, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error initializing participant: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Response content: {e.response.text}")
            return

        if not response.is_redirect:
            print(
                f"Error: Expected a redirect from {current_url}, got {response.status_code}"
            )
            print(f"Response content: {response.text}")
            return

        current_url = response.headers["Location"]
        if not current_url.startswith("http"):
            current_url = f"{OTREE_SERVER_URL}{current_url}"
        print(f"Redirected to: {current_url} (Contribute Page)")

        contribution_amount = 50
        payload = {"contribution": contribution_amount}
        print(f"POST: {current_url} with data {payload}")
        try:
            response = http_session.post(
                current_url, data=payload, allow_redirects=False, timeout=10
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error submitting contribution: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Response content: {e.response.text}")
            return

        if not response.is_redirect:
            print(
                f"Error: Expected a redirect from contribution submission, got {response.status_code}"
            )
            print(f"Response content: {response.text}")
            if "oTree-Redisplay-With-Errors" in response.headers:
                print(
                    "Form validation failed. Check oTree server logs or debug the HTML response."
                )
            return

        current_url = response.headers["Location"]
        if not current_url.startswith("http"):
            current_url = f"{OTREE_SERVER_URL}{current_url}"
        print(f"Redirected to: {current_url} (ResultsWaitPage)")

        print(f"GET: {current_url} (Accessing Wait Page)")
        try:
            response = http_session.get(current_url, allow_redirects=False, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error on ResultsWaitPage: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Response content: {e.response.text}")
            return

        if not response.is_redirect:
            if "oTree-Wait-Page" in response.headers:
                print(
                    f"Currently on wait page: {current_url}. Waiting for it to advance..."
                )
                time.sleep(5)
                try:
                    response = http_session.get(
                        current_url, allow_redirects=False, timeout=30
                    )
                    response.raise_for_status()
                except requests.exceptions.RequestException as e_retry:
                    print(f"Error on ResultsWaitPage (retry): {e_retry}")
                    return
                if not response.is_redirect:
                    print(
                        f"Still on wait page {current_url} after delay. Manual intervention or more sophisticated handling needed."
                    )
                    return
            else:
                print(
                    f"Error: Expected a redirect or a known wait page from {current_url}, got {response.status_code}"
                )
                print(f"Response content: {response.text}")
                return

        current_url = response.headers["Location"]
        if not current_url.startswith("http"):
            current_url = f"{OTREE_SERVER_URL}{current_url}"
        print(f"Redirected to: {current_url} (Results Page)")

        # 4. Submit Results Page (click-through)
        print(f"POST: {current_url} (Advancing Results Page)")
        try:
            response = http_session.post(
                current_url, data={}, allow_redirects=False, timeout=10
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error submitting results page: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Response content: {e.response.text}")
            return

        if not response.is_redirect:
            print(
                f"Error: Expected a redirect from results page, got {response.status_code}"
            )
            print(f"Response content: {response.text}")
            return

        current_url = response.headers["Location"]
        if not current_url.startswith("http"):
            current_url = f"{OTREE_SERVER_URL}{current_url}"
        print(
            f"Redirected to: {current_url} (End of Experiment / OutOfRangeNotification)"
        )

        print(f"GET: {current_url}")
        try:
            response = http_session.get(current_url, timeout=10)
            response.raise_for_status()
            print(f"Final page content snippet: {response.text[:200]}...")
        except requests.exceptions.RequestException as e:
            print(f"Error on final page: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Response content: {e.response.text}")
            return

        print(
            f"Participant {participant_id} (Code: {participant_code}) finished simulation."
        )


if __name__ == "__main__":
    try:
        session_code, participants_info = create_otree_session(
            SESSION_CONFIG_NAME, NUM_PARTICIPANTS
        )
        input("Press Enter to continue...")

        if participants_info:
            threads = []
            print(
                f"\nStarting simulation for {len(participants_info)} participants concurrently..."
            )
            for p_info in participants_info:
                thread = threading.Thread(
                    target=run_experiment_for_participant, args=(p_info,)
                )
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join()

            print("\nAll participant simulations finished.")
        else:
            print("No participants found in the created session.")

    except Exception as e:
        print(f"An error occurred in the main execution: {e}")
        import traceback

        traceback.print_exc()
