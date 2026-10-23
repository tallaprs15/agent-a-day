import subprocess


def test_echo_runs():
    # Test-only helper: runs a fixed echo, never shipped.
    subprocess.call("echo ok", shell=True)
