# Public Goods Simple

This is a simple public goods game.

# Structure of the project

The project is structured as follows:

- `server`: The oTree server code.
- `client`: The econagents client code.
- `client/bridge_server.py`: The bridge server code.
- `client/run_game.py`: The script to run the game.
- `client/state.py`: The state of the game.
- `client/roles.py`: The roles of the players.
- `client/prompts`: The prompts for the players.

## How to run

1. Create two separate virtual environments, one for the server and one for the client, install the required packages in each.
2. If you're using `uv` you can do this by going into each directory and running `uv sync`
3. Add the required `.env` files in client (see `.env.example`)
4. Run the server from the server directory: `otree devserver`
5. On a new terminal, run the `bridge_server` from the client directory: `python -m bridge_server`
6. On a new terminal, run the `run_game` from the client directory: `python -m run_game`

# What's a bridge server?

The bridge server acts as a translator between the `econagents` client and the oTree server. oTree uses standard HTTP for communication, while `econagents` clients use WebSockets. The bridge server converts WebSocket messages from clients into HTTP requests for the oTree server, and sends back responses, enabling the agent to interact with the oTree game.
