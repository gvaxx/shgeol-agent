"""API handlers — thin layer on top of db.py."""

import db
import config


def get_conn():
    return db.connect(config.DB_HOST, config.DB_PORT, config.DB_NAME, config.DB_TIMEOUT)


def handle_get_user(user_id):
    conn = get_conn()
    user = db.fetch_user(conn, user_id)
    return user


def handle_list_users():
    conn = get_conn()
    users = db.fetch_users(conn)
    return users


def handle_create_user(name, email):
    conn = get_conn()
    new_id = db.insert_user(conn, name, email)
    return {"id": new_id, "name": name, "email": email}
