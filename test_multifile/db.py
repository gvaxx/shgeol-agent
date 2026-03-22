"""Database access layer."""

import config


def connect(host, port, dbname, timeout=10):
    """Connect to the database. Returns a connection-like dict."""
    print(f"Connecting to {host}:{port}/{dbname} with timeout: {timeout}")
    return {"host": host, "port": port, "dbname": dbname, "connected": True}


def fetch_user(conn, user_id):
    """Fetch a single user by ID."""
    if not conn["connected"]:
        raise RuntimeError("Not connected")
    # Simulate DB query
    return {"id": user_id, "name": f"User_{user_id}", "email": f"user{user_id}@example.com"}


def fetch_users(conn, limit=100):
    """Fetch a list of users."""
    if not conn["connected"]:
        raise RuntimeError("Not connected")
    return [{"id": i, "name": f"User_{i}", "email": f"user{i}@example.com"} for i in range(limit)]


def insert_user(conn, name, email):
    """Insert a new user. Returns the new user's ID."""
    if not conn["connected"]:
        raise RuntimeError("Not connected")
    print(f"Inserting user: {name} <{email}>")
    return 42  # simulated new ID
