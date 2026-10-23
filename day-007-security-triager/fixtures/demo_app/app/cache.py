"""Small helpers used by the demo app."""

import hashlib
import pickle


def cache_key(url):
    # MD5 as a cache bucket name, not a security control.
    return hashlib.md5(url.encode()).hexdigest()


def load_session(cookie_value):
    # The session cookie is deserialised with pickle: remote code execution if the cookie can be forged.
    return pickle.loads(bytes.fromhex(cookie_value))


def compute(expression_table, name):
    # Looks up a fixed expression by key; the key is validated against the table first.
    return eval(expression_table[name])
