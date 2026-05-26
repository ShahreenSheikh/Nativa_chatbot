"""
Google service-account credentials helper.

Works locally with a service_account.json file or in Railway / cloud with the
GOOGLE_CREDENTIALS_JSON environment variable. Identical pattern to the Dubai
bot so the same service account can be reused if desired (just give it access
to a different Sheet ID and Calendar ID).
"""

import os
import json
from google.oauth2.service_account import Credentials


def get_credentials(scopes: list) -> Credentials:
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)
        return Credentials.from_service_account_info(creds_dict, scopes=scopes)

    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    return Credentials.from_service_account_file(file_path, scopes=scopes)
