"""
Shared database helpers for run scripts that execute inside Docker containers.

These use raw pymysql (not Django ORM) because Django is not available inside
the SC2 Docker containers.
"""

from __future__ import annotations

import os
import random

import pymysql

# Database configuration from environment variables
DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = int(os.environ.get('DB_PORT', '3306'))
DB_NAME = os.environ.get('DB_NAME', 'sc_bot')
DB_USER = os.environ.get('DB_USER', 'root')
DB_PASSWORD = os.environ.get('DB_PASSWORD', 'default')


def get_db_connection():
    """Create and return a database connection."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        autocommit=False,
    )


def update_match_result(match_id: int, result: str):
    """Update match result in the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE `match` SET end_timestamp = NOW(), result = %s WHERE id = %s',
        (result, match_id),
    )
    conn.commit()
    conn.close()


def update_match_map(match_id: int, map_name: str):
    """Update the map name for an existing match."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE `match` SET map_name = %s WHERE id = %s',
        (map_name, match_id),
    )
    conn.commit()
    conn.close()


def get_next_test_group_id() -> int:
    """Get the next test group ID by incrementing the highest completed test group ID."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT MAX(test_group_id) FROM `match` WHERE end_timestamp IS NOT NULL
    ''')

    result = cursor.fetchone()
    conn.close()

    # If no completed matches exist, start at 0, otherwise increment by 1
    return 0 if result is None else result[0] + 1


def create_pending_match(
    test_group_id: int,
    start_timestamp: str,
    map_name: str,
    opponent_race: str,
    opponent_difficulty: str,
    opponent_build: str,
) -> int | None:
    """Create a pending match entry and return the match ID."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO `match` (test_group_id, start_timestamp, map_name, opponent_race, opponent_difficulty, opponent_build, result)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (
        test_group_id,
        start_timestamp,
        map_name,
        opponent_race,
        opponent_difficulty,
        opponent_build,
        "Pending",
    ))

    match_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return match_id


def get_least_used_map(
    opponent_race: str,
    opponent_build: str,
    opponent_difficulty: str,
    map_list: list[str],
) -> str:
    """Find the map with the fewest completed matches for the given opponent config."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT map_name, count(*) ct
        FROM `match`
        WHERE opponent_race = %s
            AND opponent_build = %s
            AND opponent_difficulty = %s
            AND result IN ("Victory", "Defeat")
            AND test_group_id >= 0
        GROUP BY map_name
        ORDER BY ct
        LIMIT 1
    ''', (opponent_race, opponent_build, opponent_difficulty))

    map_name = cursor.fetchone()
    conn.close()
    return map_name[0] if map_name else random.choice(map_list)
