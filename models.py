from django.db import models


class TestGroup(models.Model):
    class Meta:
        db_table = 'test_group'

    id = models.AutoField(primary_key=True)
    description = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Group {self.id}: {self.description}" if self.description else f"Group {self.id}"


class CustomBot(models.Model):
    class Meta:
        db_table = 'custom_bot'

    Race = models.TextChoices('Race', 'Protoss Terran Zerg Random')

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)
    race = models.CharField(max_length=7, choices=Race)
    bot_file = models.CharField(
        max_length=255,
        help_text="Python filename in bot/other_bots/ (e.g. worker_rush.py)"
    )
    bot_class_name = models.CharField(
        max_length=100,
        help_text="Class name that inherits from BotAI (e.g. WorkerRushBot)"
    )
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.race} - {self.bot_class_name})"


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
        help_text="Set when the opponent is a custom bot instead of a built-in Computer"
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
    
    # Non-database attributes (computed dynamically in views)
    is_best_time: bool = False

    def __str__(self):
        if self.opponent_bot:
            return f"Group {self.test_group.id} - {self.map_name} vs {self.opponent_bot.name} ({self.result})"
        return f"Group {self.test_group.id} - {self.map_name} vs {self.opponent_race}-{self.opponent_build} ({self.result})"

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