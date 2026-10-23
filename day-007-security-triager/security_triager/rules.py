"""The Semgrep rule pack the scan always uses.

Semgrep's hosted `auto` config needs metrics switched on, so the agent ships a small, reviewable pack instead.
The callbacks inject it into every scan; the model never writes or edits rules. Each rule has a stable id that
the triage report cites, and the smoke test checks that every id fires on the demo target.
"""

from __future__ import annotations

import re

RULES_YAML = r"""
rules:
  - id: py-shell-true
    languages: [python]
    severity: ERROR
    message: subprocess call with shell=True runs its argument through the shell
    pattern: subprocess.$FUNC(..., shell=True, ...)
  - id: py-os-command
    languages: [python]
    severity: ERROR
    message: os.system / os.popen run a command string through the shell
    pattern-either:
      - pattern: os.system(...)
      - pattern: os.popen(...)
  - id: py-eval-exec
    languages: [python]
    severity: ERROR
    message: eval / exec on a dynamic value executes arbitrary code
    pattern-either:
      - pattern: eval(...)
      - pattern: exec(...)
  - id: py-pickle-load
    languages: [python]
    severity: ERROR
    message: pickle deserialisation of untrusted data executes arbitrary code
    pattern-either:
      - pattern: pickle.loads(...)
      - pattern: pickle.load(...)
  - id: py-yaml-unsafe-load
    languages: [python]
    severity: ERROR
    message: yaml.load without an explicit safe Loader can construct arbitrary objects
    patterns:
      - pattern: yaml.load($DATA)
  - id: py-weak-hash
    languages: [python]
    severity: WARNING
    message: MD5 / SHA-1 are broken for security purposes
    pattern-either:
      - pattern: hashlib.md5(...)
      - pattern: hashlib.sha1(...)
  - id: py-tls-verify-off
    languages: [python]
    severity: WARNING
    message: TLS certificate verification disabled
    pattern: requests.$FUNC(..., verify=False, ...)
  - id: py-debug-server
    languages: [python]
    severity: WARNING
    message: Web server started with debug=True exposes an interactive debugger
    pattern: $APP.run(..., debug=True, ...)
  - id: py-sql-string-building
    languages: [python]
    severity: ERROR
    message: SQL built by string formatting instead of parameters
    pattern-either:
      - pattern: $CUR.execute($Q % ...)
      - pattern: $CUR.execute($Q + ...)
      - pattern: $CUR.execute(f"...")
  - id: py-hardcoded-secret
    languages: [python]
    severity: WARNING
    message: Credential-looking name assigned a string literal
    patterns:
      - pattern: $NAME = "..."
      - metavariable-regex:
          metavariable: $NAME
          regex: (?i).*(password|passwd|secret|api_?key|token).*
  - id: js-eval
    languages: [javascript, typescript]
    severity: ERROR
    message: eval executes arbitrary code
    pattern: eval(...)
  - id: js-inner-html
    languages: [javascript, typescript]
    severity: WARNING
    message: Assigning to innerHTML can inject script if the value is user-controlled
    pattern: $EL.innerHTML = $VALUE
  - id: js-exec-concat
    languages: [javascript, typescript]
    severity: ERROR
    message: Shell command built by string concatenation
    pattern-either:
      - pattern: $CP.exec($CMD + ...)
      - pattern: $CP.exec(`...${...}...`)
"""

RULE_IDS = tuple(re.findall(r"^  - id: (\S+)", RULES_YAML, flags=re.M))
SCANNABLE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
