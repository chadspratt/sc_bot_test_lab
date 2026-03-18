#!/usr/bin/env python
"""Django management entry point for standalone test_lab quickstart.

Run this from inside the test_lab/ directory:

    python quickstart/manage.py runserver

It adds the *parent* of test_lab/ to sys.path so that ``import test_lab``
works without installing the package.
"""

import os
import sys


def main():
    # Ensure the parent of test_lab/ is on the path so Django can
    # import the 'test_lab' package by name.
    test_lab_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent_dir = os.path.dirname(test_lab_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'test_lab.quickstart.settings')

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
