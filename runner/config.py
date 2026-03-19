"""
Shared configuration for run scripts that execute inside Docker containers.

Maps, race/difficulty/build dictionaries, and other constants used by
multiple runner scripts.
"""

from sc2.data import AIBuild, Difficulty, Race

MAP_LIST = [
    "PersephoneAIE_v4",
    "IncorporealAIE_v4",
    "PylonAIE_v4",
    "TorchesAIE_v4",
    "UltraloveAIE_v2",
    "MagannathaAIE_v2",
]

RACE_DICT = {
    None: Race.Random,
    "random": Race.Random,
    "protoss": Race.Protoss,
    "terran": Race.Terran,
    "zerg": Race.Zerg,
}

BUILD_DICT = {
    None: AIBuild.RandomBuild,
    "rush": AIBuild.Rush,
    "timing": AIBuild.Timing,
    "macro": AIBuild.Macro,
    "power": AIBuild.Power,
    "air": AIBuild.Air,
    "randombuild": AIBuild.RandomBuild,
}

DIFFICULTY_DICT = {
    None: Difficulty.CheatInsane,
    "Easy": Difficulty.Easy,
    "Medium": Difficulty.Medium,
    "MediumHard": Difficulty.MediumHard,
    "Hard": Difficulty.Hard,
    "Harder": Difficulty.Harder,
    "VeryHard": Difficulty.VeryHard,
    "CheatVision": Difficulty.CheatVision,
    "CheatMoney": Difficulty.CheatMoney,
    "CheatInsane": Difficulty.CheatInsane,
}
