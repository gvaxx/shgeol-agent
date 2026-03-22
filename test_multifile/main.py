"""Entry point."""

import sys
import api


def main():
    if len(sys.argv) < 2:
        print("Commands: get <id> | list | create <name> <email>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "get":
        user_id = int(sys.argv[2])
        user = api.handle_get_user(user_id)
        print(user)

    elif cmd == "list":
        users = api.handle_list_users()
        for u in users:
            print(u)

    elif cmd == "create":
        name  = sys.argv[2]
        email = sys.argv[3]
        result = api.handle_create_user(name, email)
        print(result)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
