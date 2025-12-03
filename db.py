# db.py
import os
import time
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "telegrambot")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable not set")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

warnings_col = db["warnings"]
filters_col = db["filters"]
settings_col = db["settings"]
logs_col = db["logs"]

# WARNINGS
def get_warn_count(chat_id, user_id):
    doc = warnings_col.find_one({"chat_id": int(chat_id), "user_id": int(user_id)})
    return doc["count"] if doc else 0

def set_warn_count(chat_id, user_id, count):
    warnings_col.update_one(
        {"chat_id": int(chat_id), "user_id": int(user_id)},
        {"$set": {"count": int(count), "last_warn_ts": int(time.time())}},
        upsert=True
    )

def reset_warn(chat_id, user_id):
    warnings_col.delete_one({"chat_id": int(chat_id), "user_id": int(user_id)})

# FILTERS
def add_filter(chat_id, word):
    filters_col.update_one(
        {"chat_id": int(chat_id), "word": word},
        {"$set": {"chat_id": int(chat_id), "word": word}},
        upsert=True
    )

def remove_filter(chat_id, word):
    filters_col.delete_one({"chat_id": int(chat_id), "word": word})

def get_filters(chat_id):
    docs = filters_col.find({"chat_id": int(chat_id)})
    return [d["word"] for d in docs]

# SETTINGS
def get_setting(chat_id, key, default=None):
    doc = settings_col.find_one({"chat_id": int(chat_id)})
    if doc and key in doc:
        return doc[key]
    return default

def set_setting(chat_id, key, value):
    settings_col.update_one(
        {"chat_id": int(chat_id)},
        {"$set": {key: value}},
        upsert=True
    )

# LOGS
def log_action(chat_id, user_id, action, reason=""):
    logs_col.insert_one({
        "chat_id": int(chat_id),
        "user_id": int(user_id) if user_id is not None else None,
        "action": action,
        "reason": reason,
        "timestamp": int(time.time())
    })
