"""Plugin: Building Timing.

Shows the earliest time (in game seconds) each building type was completed,
aggregated per test group, with colour-coded performance vs the average.
"""

from collections import defaultdict
from typing import List

from django.db.models import Min

name = 'Building Timing'
description = 'Shows earliest building construction times per test group.'
fullpage = True
template = 'test_lab/plugins/building_timing.html'


def get_context(request) -> dict:
    from test_lab.models import MatchEvent

    building_events = (
        MatchEvent.objects
        .filter(type='Building')
        .values('match__test_group_id', 'match_id', 'message', 'match__result')
        .annotate(earliest_time=Min('game_timestamp'))
        .order_by('match__test_group_id', 'message')
    )

    # {test_group_id: {building_type: {min, max, avg, count, min_result, max_result}}}
    grouped_data = defaultdict(dict)
    all_building_types = set()

    for event in building_events:
        test_group_id = event['match__test_group_id']
        building_type = event['message']
        earliest_time = event['earliest_time']
        result = event['match__result'][0]

        if building_type not in grouped_data[test_group_id]:
            grouped_data[test_group_id][building_type] = {
                "min": earliest_time,
                "max": earliest_time,
                "avg": earliest_time,
                "count": 1,
                "min_result": result,
                "max_result": result,
            }
            all_building_types.add(building_type)
        else:
            current = grouped_data[test_group_id][building_type]
            if earliest_time < current["min"]:
                current["min"] = earliest_time
                current["min_result"] = result
            if earliest_time > current["max"]:
                current["max"] = earliest_time
                current["max_result"] = result
            current["avg"] += earliest_time
            current["count"] += 1

    for test_group_id in grouped_data:
        for building_type in grouped_data[test_group_id]:
            current = grouped_data[test_group_id][building_type]
            current["avg"] = current["avg"] / current["count"]
            del current["count"]

    building_types_list = list(all_building_types)
    sorted_groups = sorted(grouped_data.keys(), reverse=True)

    avg_timings = []
    for building_type in building_types_list:
        timings: List[float | None] = [
            grouped_data[gid].get(building_type).get("avg")  # type: ignore
            for gid in sorted_groups
            if grouped_data[gid].get(building_type) is not None
        ]
        if timings:
            avg_timings.append(sum(timings) / len(timings))  # type: ignore
        else:
            avg_timings.append(None)

    sorted_building_types, avg_timings = zip(
        *sorted(zip(building_types_list, avg_timings), key=lambda x: x[1])
    )
    avg_timing_dict = dict(zip(sorted_building_types, avg_timings))

    pivot_data = []
    for group_id in sorted_groups:
        row = {'test_group_id': group_id, 'timings': []}
        for building_type in sorted_building_types:
            timing = grouped_data[group_id].get(building_type)
            if timing and avg_timing_dict.get(building_type):
                avg = avg_timing_dict[building_type]
                diff = timing['avg'] - avg
                if diff < -10:
                    performance_class = 'much-faster'
                elif diff < -5:
                    performance_class = 'faster'
                elif diff < 0:
                    performance_class = 'slightly-faster'
                elif diff > 10:
                    performance_class = 'much-slower'
                elif diff > 5:
                    performance_class = 'slower'
                elif diff > 0:
                    performance_class = 'slightly-slower'
                else:
                    performance_class = 'average'
                timing['performance_class'] = performance_class
            row['timings'].append(timing)
        pivot_data.append(row)

    return {
        'pivot_data': pivot_data,
        'building_types': sorted_building_types,
        'avg_timings': avg_timings,
    }
