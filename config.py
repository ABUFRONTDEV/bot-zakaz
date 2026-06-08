import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

MIN_PLAYERS = 4
MAX_PLAYERS = 15

NIGHT_TIME = 60        # seconds for night phase
DISCUSSION_TIME = 90   # seconds for day discussion
VOTE_TIME = 60         # seconds for voting
