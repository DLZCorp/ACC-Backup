"""
Shared requests session for ACC-Backup.

Uses the corporate Zscaler CA bundle (set via REQUESTS_CA_BUNDLE env var)
for SSL verification on all outbound HTTPS requests.
"""

import os
import requests

_CA_BUNDLE = os.environ.get(
    "REQUESTS_CA_BUNDLE",
    r"C:\Users\wguethlein\AppData\Roaming\ca-bundle.pem"
)

session = requests.Session()
session.verify = _CA_BUNDLE