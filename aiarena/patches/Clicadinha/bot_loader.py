import sys

sys.path.append("ares-sc2/src/ares")
sys.path.append("ares-sc2/src")
sys.path.append("ares-sc2")
sys.path.append("queens-sc2")

from bot.main import MyBot

BOT_NAME = "Clicadinha"
BOT_DEFAULT_RACE = "Zerg"


def create_bot():
    return MyBot()
