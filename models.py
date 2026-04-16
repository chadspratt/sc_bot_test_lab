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
        help_text="Entry point filename (e.g. 'run.py', 'RustyNikolaj'). Passed as BOT_ENTRY to the Docker runner.",
    )
    bot_class_name = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Deprecated: kept for backward compatibility with older bot registrations.",
    )
    bot_module = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Python module path for dynamic import (e.g. 'bottato.bottato'). Used by the single-container Docker runner.",
    )
    bot_class = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Python class name that inherits from BotAI (e.g. 'BotTato'). Used by the single-container Docker runner.",
    )
    is_external = models.BooleanField(
        default=False,
        help_text="Deprecated: kept for backward compatibility.",
    )
    bot_directory = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="Directory name under aiarena/bots/",
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
        help_text="Absolute path to the bot's live development source directory on the host, which may differ from the packaged version in the bots folder",
    )
    enable_version_history = models.BooleanField(
        default=False,
        help_text="Enable past-version matches for this bot (requires source_path to point to a git repo)",
    )
    archive_paths = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Paths to extract from git history when testing against past versions. "
            "E.g. ['src/', 'bot.py', 'config/']. If empty, the entire tree is archived."
        ),
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
        help_text="Custom Dockerfile (relative to test_lab/aiarena/) that builds an image with extra dependencies pre-installed, e.g. Dockerfile.mybot. Saves time when running this bot repeatedly by avoiding per-match installs.",
    )
    env_file = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="Absolute path to a .env file on the host. Passed as --env-file to docker compose run, providing environment variables (e.g. DB credentials) to the container.",
    )
    default_test_suite = models.ForeignKey(
        'TestSuite',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='default_for_bots',
        help_text="Default test suite used when triggering tests for this bot without specifying a suite",
    )
    default_test_suite_id: int | None
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive bots are excluded from all test suite runs",
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
    is_protected = models.BooleanField(
        default=False,
        help_text="Protected suites cannot be edited or deleted from the UI",
    )
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
    include_all_custom_bots = models.BooleanField(
        default=False,
        help_text="Include all active custom bots instead of a specific selection",
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
    custom_bot_builds = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Per-bot build config overrides: {"<bot_id>": ["build1", ...]}. '
            'When a bot ID is present, one match per listed build is launched '
            'during test suite runs.'
        ),
    )
    map_name = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Force all matches in this suite to use a specific map. Empty = auto-select.',
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
    branch = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text="Git branch the test was run against. Empty = current working directory (default).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        label = f"Group {self.id}"
        if self.description:
            label += f": {self.description}"
        if self.branch:
            label += f" [{self.branch}]"
        return label


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
    opponent_build = models.CharField(max_length=100, blank=True, default='')
    opponent_bot = models.ForeignKey(
        CustomBot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='opponent_matches',
        help_text="Set when the opponent is a custom bot instead of a built-in Computer"
    )
    test_bot = models.ForeignKey(
        CustomBot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='test_matches',
        help_text="The bot being tested (Player 1).",
    )
    result = models.CharField(max_length=50, choices=Result)
    duration_in_game_time = models.IntegerField(null=True, blank=True)
    friendly_race = models.CharField(
        max_length=7, blank=True, default='',
        help_text="Race selected for this match. Set pre-match for Random-race bots, or resolved post-match from log.",
    )
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
    friendly_build = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Build config name used by the test bot (from aiarena/configs/). Empty = default config.",
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
    state_db_file = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="Path to a state snapshot SQLite DB file on the host. Mounted into the container so the bot can restore mid-game state.",
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


class PromptTemplate(models.Model):
    """Reusable prompt template for ticket-based agent work.

    Template content is stored as .md files in test_lab/prompt_templates/.
    This model tracks the filename and bot registrations.
    """

    class Meta:
        db_table = 'prompt_template'

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    filename = models.CharField(
        max_length=200,
        unique=True,
        help_text="Filename (relative to test_lab/prompt_templates/) e.g. mybot.md",
    )
    bots = models.ManyToManyField(
        CustomBot,
        blank=True,
        related_name='prompt_templates',
        help_text="Bots this template is registered for. If empty, it is a generic/default template available to bots with no registered templates.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Ticket(models.Model):
    """A unit of work describing a change to make to a bot."""

    class Meta:
        db_table = 'ticket'
        ordering = ['-created_at']

    Status = models.TextChoices('Status',
        'draft ready in_progress review testing done rejected')

    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=200)
    description = models.TextField(
        help_text="Detailed spec: what to change, acceptance criteria, files to focus on",
    )
    status = models.CharField(
        max_length=20, choices=Status, default='draft',
    )
    branch = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Auto-generated branch name, e.g. ticket/42-improve-kiting",
    )
    test_bot = models.ForeignKey(
        CustomBot, on_delete=models.CASCADE,
        related_name='tickets',
        help_text="Which bot to modify",
    )
    test_suite = models.ForeignKey(
        TestSuite, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tickets',
        help_text="Which test suite to run when work is done",
    )
    prompt_template = models.ForeignKey(
        'PromptTemplate', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tickets',
        help_text="Prompt template to use when generating the .prompt.md file",
    )
    prompt_template_id: int | None
    context_files = models.TextField(
        blank=True, default='',
        help_text="Newline-separated list of files the agent should focus on",
    )
    prompt_file = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Path to the generated .prompt.md file",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def slug(self) -> str:
        """URL-safe slug derived from the title."""
        import re
        slug = self.title.lower().strip()
        slug = re.sub(r'[^a-z0-9]+', '-', slug)
        return slug.strip('-')[:50]

    @property
    def branch_name(self) -> str:
        """Return the branch name, auto-generating if empty."""
        if self.branch:
            return self.branch
        return f'ticket-{self.id}-{self.slug}'

    def __str__(self):
        return f"#{self.id}: {self.title} [{self.status}]"


class SystemConfig(models.Model):
    """Singleton table for system-wide settings."""

    class Meta:
        db_table = 'system_config'

    id = models.AutoField(primary_key=True)
    max_concurrent_custom_bots = models.IntegerField(
        default=0,
        help_text="Maximum number of custom bot instances that can run at the same time. "
                  "vs Blizzard AI and replay tests count as 1; vs custom bot and vs past version count as 2. "
                  "0 = unlimited.",
    )
    sc2_switcher_path = models.CharField(
        max_length=500,
        blank=True,
        default=r'C:\Program Files (x86)\StarCraft II\Support\SC2Switcher.exe',
        help_text="Path to SC2Switcher.exe for opening replays.",
    )
    sc2_maps_path = models.CharField(
        max_length=500,
        blank=True,
        default=r'C:\Program Files (x86)\StarCraft II\Maps',
        help_text="Host path to StarCraft II Maps directory (mounted into Docker containers).",
    )

    @property
    def is_configured(self) -> bool:
        """Return True if all required fields have been set."""
        return bool(self.sc2_maps_path)

    def __str__(self):
        return f"SystemConfig (max_concurrent_custom_bots={self.max_concurrent_custom_bots})"

    @classmethod
    def load(cls) -> 'SystemConfig':
        """Return the single SystemConfig row, creating it if needed."""
        obj, _ = cls.objects.get_or_create(id=1)
        return obj