import os

from django.db import models


class CustomBot(models.Model):
    class Meta:
        db_table = 'custom_bot'

    Race = models.TextChoices('Race', 'Protoss Terran Zerg Random')

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)
    race = models.CharField(max_length=7, choices=Race)
    bot_type = models.CharField(
        max_length=20,
        default='aiarena',
        help_text="Bot type — currently only 'aiarena' is supported.",
    )
    bot_file = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Deprecated: kept for backward compatibility with older bot registrations.",
    )
    bot_class_name = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Deprecated: kept for backward compatibility with older bot registrations.",
    )
    is_external = models.BooleanField(
        default=False,
        help_text="Deprecated: kept for backward compatibility.",
    )
    bot_directory = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text=(
            "Directory name under other_bots/ (external_python) "
            "or under aiarena/bots/ (aiarena type)"
        ),
    )
    aiarena_bot_type = models.CharField(
        max_length=20,
        blank=True,
        default='python',
        help_text="Bot type for aiarena matches file: python, cppwin32, cpplinux, dotnetcore, java, nodejs, etc.",
    )
    is_test_subject = models.BooleanField(
        default=False,
        help_text="Whether this bot can be used as Player 1 (the bot being tested/developed)",
    )
    source_path = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="Absolute path to the bot's live source directory on the host",
    )
    git_repo_path = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="Absolute path to the bot's git repository (for past version support)",
    )
    enable_version_history = models.BooleanField(
        default=False,
        help_text="Enable past-version matches for this bot (requires git_repo_path)",
    )
    symlink_mounts = models.JSONField(
        default=list,
        blank=True,
        help_text='Detected symlinks needing separate Docker mounts: [{"name": "...", "target": "..."}]',
    )
    dockerfile = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Custom Dockerfile name in aiarena/ for mirror/past-version matches (e.g. Dockerfile.bottato)",
    )
    default_test_suite = models.ForeignKey(
        'TestSuite',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='default_for_bots',
        help_text="Default test suite used when triggering tests for this bot without specifying a suite",
    )
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_aiarena(self) -> bool:
        return self.bot_type == 'aiarena'

    def __str__(self):
        if self.is_aiarena:
            return f"{self.name} ({self.race} - aiarena)"
        return f"{self.name} ({self.race} - {self.bot_class_name})"


class TestSuite(models.Model):
    class Meta:
        db_table = 'test_suite'

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)
    include_blizzard_ai = models.BooleanField(
        default=True,
        help_text="Include the 15 built-in AI matches (3 races x 5 builds)",
    )
    custom_bots = models.ManyToManyField(
        CustomBot,
        blank=True,
        related_name='test_suites',
        help_text="Custom bots to include in this test suite",
    )
    previous_versions = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text=(
            "Comma-separated version offsets for past-version matches. "
            "E.g. '1,3' runs against the 1st and 3rd most recent previous commits."
        ),
    )
    replay_tests = models.ManyToManyField(
        'ReplayTest',
        blank=True,
        related_name='test_suites',
        help_text="Replay tests to include in this test suite",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def previous_version_offsets(self) -> list[int]:
        """Parse previous_versions string into a sorted list of positive ints."""
        if not self.previous_versions:
            return []
        offsets = []
        for part in self.previous_versions.split(','):
            part = part.strip()
            if part.isdigit() and int(part) >= 1:
                offsets.append(int(part))
        return sorted(set(offsets))

    def __str__(self):
        return self.name


class TestGroup(models.Model):
    class Meta:
        db_table = 'test_group'

    id = models.AutoField(primary_key=True)
    description = models.CharField(max_length=255, blank=True, default='')
    test_suite = models.ForeignKey(
        TestSuite, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='test_groups',
        help_text="The test suite configuration used for this test group",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Group {self.id}: {self.description}" if self.description else f"Group {self.id}"


class Match(models.Model):
    class Meta:
        db_table = 'match'

    Race = models.TextChoices('Race','Protoss Terran Zerg Random')
    Difficulty = models.TextChoices('Difficulty',
        'Easy Medium MediumHard Hard Harder VeryHard CheatVision CheatMoney CheatInsane')
    Build = models.TextChoices('Build', 'Air Macro Power Rush Timing RandomBuild')
    Result = models.TextChoices('Result', 'Victory Defeat Tie Undecided')

    id = models.AutoField(primary_key=True)
    test_group = models.ForeignKey(TestGroup, on_delete=models.CASCADE)
    start_timestamp = models.DateTimeField()
    end_timestamp = models.DateTimeField(null=True, blank=True)
    map_name = models.CharField(max_length=100)
    opponent_race = models.CharField(max_length=7, choices=Race)
    opponent_difficulty = models.CharField(max_length=11, choices=Difficulty, blank=True, default='')
    opponent_build = models.CharField(max_length=15, choices=Build, blank=True, default='')
    opponent_bot = models.ForeignKey(
        CustomBot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='opponent_matches',
        help_text="Set when the opponent is a custom bot instead of a built-in Computer"
    )
    test_bot = models.ForeignKey(
        CustomBot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='test_matches',
        help_text="The bot being tested (Player 1). NULL = BotTato (legacy).",
    )
    result = models.CharField(max_length=50, choices=Result)
    duration_in_game_time = models.IntegerField(null=True, blank=True)
    replay_file = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Path to uploaded replay file for continue-from-replay matches"
    )
    replay_takeover_game_loop = models.IntegerField(
        null=True, blank=True,
        help_text="Game loop at which bots take over from the replay"
    )
    opponent_commit_hash = models.CharField(
        max_length=40, blank=True, default='',
        help_text="Git commit hash of the bot version used as opponent (past-version matches)"
    )
    replay_test = models.ForeignKey(
        'ReplayTest', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='matches',
        help_text="The replay test that generated this match (auto-set by test suite runner)",
    )

    # Non-database attributes (computed dynamically in views)
    is_best_time: bool = False

    @property
    def opponent_short_hash(self) -> str:
        """Return the first 7 characters of the opponent commit hash."""
        return self.opponent_commit_hash[:7] if self.opponent_commit_hash else ''

    @property
    def opponent_version_bot_name(self) -> str:
        """Return the aiarena bot name for a past-version opponent."""
        if self.opponent_commit_hash:
            prefix = self.test_bot.bot_directory if self.test_bot else 'BotTato'
            return f'{prefix}_v_{self.opponent_commit_hash[:7]}'
        return ''

    @property
    def test_bot_name(self) -> str:
        """Display name for the bot being tested."""
        return self.test_bot.name if self.test_bot else 'BotTato'

    @property
    def test_bot_directory(self) -> str:
        """Aiarena directory name of the bot being tested (for log paths)."""
        return self.test_bot.bot_directory if self.test_bot else 'BotTato'

    def __str__(self):
        if self.opponent_commit_hash:
            return f"Group {self.test_group.id} - {self.map_name} vs BotTato@{self.opponent_commit_hash[:7]} ({self.result})"
        if self.opponent_bot:
            return f"Group {self.test_group.id} - {self.map_name} vs {self.opponent_bot.name} ({self.result})"
        return f"Group {self.test_group.id} - {self.map_name} vs {self.opponent_race}-{self.opponent_build} ({self.result})"

class ReplayTest(models.Model):
    class Meta:
        db_table = 'replay_test'

    OpponentType = models.TextChoices('OpponentType', 'BuiltInAI CustomBot')

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=200)
    replay_file = models.CharField(
        max_length=500,
        help_text="Path to the .SC2Replay file on the host",
    )
    start_time = models.CharField(
        max_length=20,
        help_text="Game clock time to start from, e.g. '2:41'",
    )
    duration = models.CharField(
        max_length=20,
        help_text="How long to run before the bot forfeits, e.g. '3:00'",
    )
    bot_player_id = models.IntegerField(
        default=1,
        help_text="Which player in the replay the test bot takes over (1 or 2)",
    )
    opponent_type = models.CharField(
        max_length=12,
        choices=OpponentType,
        default='BuiltInAI',
        help_text="Whether the opponent is a built-in AI or a custom bot",
    )
    opponent_race = models.CharField(
        max_length=7,
        choices=Match.Race,
        default='Random',
        help_text="Opponent race for built-in AI matches",
    )
    opponent_difficulty = models.CharField(
        max_length=11,
        choices=Match.Difficulty,
        default='CheatInsane',
        help_text="Opponent difficulty for built-in AI matches",
    )
    opponent_build = models.CharField(
        max_length=15,
        choices=Match.Build,
        default='Macro',
        help_text="Opponent build for built-in AI matches",
    )
    opponent_bot = models.ForeignKey(
        CustomBot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='replay_tests',
        help_text="Custom bot to use as the opponent (when opponent_type is CustomBot)",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def replay_filename(self) -> str:
        return os.path.basename(self.replay_file)

    def __str__(self):
        return f"{self.name} ({self.start_time} +{self.duration})"


class MatchEvent(models.Model):
    class Meta:
        db_table = 'match_event'

    id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE)
    type = models.CharField(max_length=50)
    message = models.TextField()
    game_timestamp = models.FloatField()

    def __str__(self):
        return f"Match {self.match.id} {self.type} Event at {self.game_timestamp}: {self.message}"