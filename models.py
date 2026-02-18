from django.db import models


class TestGroup(models.Model):
    class Meta:
        db_table = 'test_group'

    id = models.AutoField(primary_key=True)
    description = models.CharField(max_length=255, blank=True, default='')
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
    opponent_difficulty = models.CharField(max_length=11, choices=Difficulty)
    opponent_build = models.CharField(max_length=15, choices=Build)
    result = models.CharField(max_length=50, choices=Result)
    duration_in_game_time = models.IntegerField(null=True, blank=True)
    
    # Non-database attributes (computed dynamically in views)
    is_best_time: bool = False

    def __str__(self):
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