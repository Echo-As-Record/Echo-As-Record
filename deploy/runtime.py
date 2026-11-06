"""Container-only secret loading; no dotenv discovery, no credential diagnostics."""

import os
import sys
from pathlib import Path

from psycopg.conninfo import make_conninfo


def main():
    try:
        password = Path("/run/secrets/service_db_password").read_text().strip()
        if not password:
            raise ValueError
        os.environ["EAR_STORAGE_DSN"] = make_conninfo(
            host="postgres", port=5432, dbname="ear", user="ear_storage", password=password
        )
        if os.environ.get("EAR_NATS_SERVERS"):
            token = Path("/run/secrets/nats_token").read_text().strip()
            if not token:
                raise ValueError
            os.environ["EAR_NATS_TOKEN"] = token
        if sys.argv[1:2] == ["manage-worker"]:
            from ear_storage.deployment import read_identity_tokens

            tokens = read_identity_tokens(Path("/run/secrets/ear/identities.env").read_text())
            os.environ["EAR_MANAGEMENT_WORKER_TOKEN"] = tokens["EAR_MANAGEMENT_WORKER_TOKEN"]
        os.execvp("ear-storage", ["ear-storage", *sys.argv[1:]])
    except Exception:
        sys.exit("storage container configuration unavailable")


if __name__ == "__main__":
    main()
