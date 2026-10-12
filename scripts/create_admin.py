"""

Create an admin, or promote an existing account.

python -m scripts.create_admin you@example.com "Your Name"

"""
import getpass
import sys

import psycopg

from app.config import get_settings
from app.security import hash_password


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit('usage: python -m scripts.create_admin EMAIL "FULL NAME"')
    email, name = sys.argv[1].strip().lower(), sys.argv[2].strip()
    password = getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        sys.exit("Password too short.")
    with psycopg.connect(get_settings().database_url, prepare_threshold=None) as conn:
        conn.execute(
            """insert into profiles (email, password_hash, full_name, role) values (%s, %s, %s, 'admin')
               on conflict (email) do update set role = 'admin', password_hash = excluded.password_hash""",
            (email, hash_password(password), name))
    print(f"{email} is now an admin.")


if __name__ == "__main__":
    main()
