#!/usr/bin/env python
"""Django command-line entry point.

Every management command runs through here: migrate, runserver, test, and this
project's own commands - ingest (PDF -> index), evaluate (golden-set validation),
doctor (environment checkup).
"""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
