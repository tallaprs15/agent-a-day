# Security triage: demo_app
## Summary
The codebase contains several high-risk injection and deserialisation flaws that require immediate remediation, particularly OS command injection in `ping_host`. F5 should be fixed first because it directly executes attacker-supplied input via a shell pipe.

## Findings
| ID | Rule | Location | Verdict | Why | Fix |
|---|---|---|---|---|---|
| F1 | py-weak-hash | app/cache.py:9 | false positive | MD5 is used solely for cache bucket naming in `cache_key(url)`, not for cryptography or security. | None needed (non-security use). |
| F2 | py-pickle-load | app/cache.py:14 | likely | `load_session` deserialises cookies via `pickle.loads(bytes.fromhex(cookie_value))`, but cookie generation/signing mechanism is not visible in the read files. | Use `json.loads` or a cryptographically signed safe serializer. |
| F3 | py-eval-exec | app/cache.py:19 | false positive | `eval` runs on a value looked up by `name` from `expression_table`, where keys are validated first. | Refactor to avoid `eval` or use a safe expression parser. |
| F4 | py-tls-verify-off | app/client.py:8 | exploitable | `verify=False` is passed to `requests.get` while sending the `"Authorization": "Bearer partner"` header, exposing partner API calls to MITM. | Remove `verify=False` or set `verify=True`. |
| F5 | py-os-command | app/views.py:12 | exploitable | `host = request.args.get("host")` is concatenated directly into `os.popen("ping -c 1 " + host).read()`. | Use `subprocess.run(["ping", "-c", "1", host], check=True)` without `shell=True`. |
| F6 | py-shell-true | app/views.py:17 | false positive | `subprocess.call` runs a hardcoded constant string `"logrotate /etc/logrotate.d/demo"` with no user input. | Remove `shell=True` or pass arguments as a list. |
| F7 | py-yaml-unsafe-load | app/views.py:22 | exploitable | `yaml.load(request.body)` is called on request body input without a safe loader, allowing arbitrary object construction. | Change to `yaml.safe_load(request.body)`. |

## Not covered
`tests/`, `README.md`, and `app/__init__.py` were not read or scanned.

---
Verdicts (counted from the table above): 3 exploitable, 1 likely, 0 unlikely, 3 false positive
