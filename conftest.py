"""Pytest configuration for the backup scripts.

The scripts are flat modules in the repository root rather than a package.
The presence of this file makes pytest put the root on sys.path so the tests
can import them directly.
"""
