from pathlib import Path

from dotenv import load_dotenv

# Load the agent's .env (git-ignored) so the live test runs when a key is present.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
