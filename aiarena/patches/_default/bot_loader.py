"""Generic bot_loader.py template — edit the TODOs below before use.

This file is patched into newly registered bots that don't have a
bot-specific patch.  Both run_vs_computer.py and run_from_replay.py
check for it and use it to create the bot instance, along with setting some global variables.

For public bots, you can add the patch and commit it back to the repo
"""

# TODO: add any sys.path inserts needed for the bot's dependencies
# import sys
# sys.path.insert(1, "some_dependency_dir")

# TODO: replace with the bot's actual import
# from my_bot_package.my_bot_module import MyBotClass
raise ImportError(
    "Generic bot_loader.py template — edit the import and "
    "TODOs before use.  See patches/_default/bot_loader.py"
)

# TODO: set the bot's display name and default race
BOT_NAME = "CHANGE_ME"
BOT_DEFAULT_RACE = "Random"


def create_bot():
    # TODO: replace with the bot's constructor
    return MyBotClass()  # noqa: F821
