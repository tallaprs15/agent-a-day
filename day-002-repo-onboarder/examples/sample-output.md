# Onboarding guide: modelcontextprotocol/python-sdk

## What this project is
The Model Context Protocol (MCP) Python SDK enables developers to seamlessly integrate Large Language Models (LLMs) with external data sources, tools, and prompts. It provides robust server frameworks for exposing capabilities and client components for connecting to and interacting with MCP servers in a standardized and secure manner.

## Architecture map
| Area | Path | What lives there |
| :--- | :--- | :--- |
| Core SDK | [`src/mcp`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/src/mcp) | Server implementations, client components, shared logic, and transports. |
| Protocol Types | [`src/mcp-types`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/src/mcp-types) | Standalone definitions for core MCP types and schemas. |
| Documentation | [`docs`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/docs) | Project documentation, user guides, and tutorials. |
| Runnable Examples | [`docs_src`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/docs_src) | Code examples embedded in documentation and used in test suites. |
| Code Examples | [`examples`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/examples) | Standalone example snippets and implementations. |
| Test Suite | [`tests`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/tests) | Unit and integration test suites. |
| Scripts | [`scripts`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/scripts) | Maintenance and utility scripts (e.g., test runner, doc generation). |
| CI Workflows | [`.github/workflows`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/.github/workflows) | GitHub Actions CI/CD definitions. |

## Where to start reading
1. [`docs/get-started/first-steps.md`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/docs/get-started/first-steps.md) — Introduces fundamental concepts (hosts, clients, servers, primitives) with runnable examples.
2. [`src/mcp/server/mcpserver/server.py`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/src/mcp/server/mcpserver/server.py) — Defines the high-level `MCPServer` decorator-based API (`@mcp.tool()`, etc.).
3. [`src/mcp/server/mcpserver/context.py`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/src/mcp/server/mcpserver/context.py) — Explains the injected `Context` object used for logging, progress reporting, and request metadata.
4. [`src/mcp/client/__init__.py`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/src/mcp/client/__init__.py) — Exposes the `Client` class used for in-memory testing of servers.
5. [`src/mcp/server/lowlevel/server.py`](https://github.com/modelcontextprotocol/python-sdk/blob/HEAD/src/mcp/server/lowlevel/server.py) — Outlines the low-level protocol server implementation and request dispatching.

## Local setup
```bash
# Prerequisites: Python 3.10+ and uv installed
git clone https://github.com/modelcontextprotocol/python-sdk.git
cd python-sdk
uv sync --frozen --all-extras --dev
uv tool install pre-commit --with pre-commit-uv --force-reinstall

# Run code quality checks
uv run pyright
uv run ruff check .
uv run ruff format .

# Run tests
uv run pytest
./scripts/test
```

## How to contribute
- **Branching**: Target `main` for V2 development and new features. Target `v1.x` for stable V1 bug fixes and security updates (use `[v1.x]` title prefix on backport PRs).
- **Issues**: All non-trivial pull requests require a linked GitHub issue.
- **AI Disclosure**: Disclose any AI assistance used in your PR description.
- **Code Standards**: Strict type hints via `pyright` (strict mode) and complete docstrings (including `Raises:` sections) are required. 100% test coverage is strictly enforced.
- **Pre-commit**: Ensure pre-commit hooks pass locally before submitting code.

## Gotchas
- **Package Management**: Only `uv` is allowed. `pip`, `uv pip install`, and `@latest` syntax are strictly forbidden.
- **Test Styles**: Never use `Test`-prefixed classes; write plain top-level `test_*` functions instead.
- **Timeouts & Delays**: Avoid `anyio.sleep()` unless specifically testing time-based features. Indefinite waits must always be bounded with `anyio.fail_after(5)`.
- **Suppressions & Exceptions**: Do not add new `# pragma: no cover`, `# type: ignore`, or `# noqa` comments. Always use `logger.exception()` when catching exceptions, and avoid bare `except Exception:` handlers except at top-level boundaries.