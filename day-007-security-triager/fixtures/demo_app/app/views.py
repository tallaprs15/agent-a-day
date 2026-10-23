"""Demo web handlers. Deliberately flawed: this is the target the triager is pointed at."""

import os
import subprocess

import yaml


def ping_host(request):
    # Attacker-controlled "host" goes straight into a shell command.
    host = request.args.get("host")
    return os.popen("ping -c 1 " + host).read()


def rotate_logs():
    # Constant command, no user input: flagged by pattern, not exploitable.
    subprocess.call("logrotate /etc/logrotate.d/demo", shell=True)


def import_settings(request):
    # Untrusted upload parsed with the unsafe YAML loader.
    return yaml.load(request.body)
