import pytest

from web_qa_explorer.risk import in_scope, is_risky

TARGET = "https://shop.example.com/app"


@pytest.mark.parametrize(
    "args",
    [
        {"element": "Delete button for todo 'Buy milk'", "target": "e12"},
        {"element": "Delete 'Buy milk'", "target": "e12"},
        {"element": "Clear completed button", "target": "e30"},
        {"element": "Place order", "target": "e5"},
        {"element": "Log out link", "target": "e2"},
        {"element": "Password field", "target": "e7", "text": "hunter2"},
        {"element": "Card number", "target": "e8", "text": "4242"},
        {"element": "Send message", "target": "e9", "text": "hi"},
    ],
)
def test_state_changing_or_sensitive_actions_are_risky(args):
    assert is_risky(args)


@pytest.mark.parametrize(
    "args",
    [
        {"element": "Toggle todo 'Buy milk'", "target": "e11"},
        {"element": "Search box", "target": "e3", "text": "red shoes"},
        {"element": "Active filter link", "target": "e31"},
        {"element": "What needs to be done? input", "target": "e8", "text": "Buy milk", "submit": True},
    ],
)
def test_ordinary_interactions_are_not_risky(args):
    assert not is_risky(args)


def test_scope_allows_the_target_host_and_subdomains_only():
    assert in_scope("https://shop.example.com/cart", TARGET)
    assert in_scope("https://api.shop.example.com/x", TARGET)
    assert not in_scope("https://example.com/", TARGET)
    assert not in_scope("https://evilshop.example.com.attacker.io/", TARGET)
    assert not in_scope("https://notshop.example.com/", TARGET)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "http://169.254.169.254/latest/meta-data/", "http://localhost:8080/"])
def test_scope_blocks_non_http_and_internal_addresses(url):
    assert not in_scope(url, TARGET)


def test_scope_allows_a_local_target_for_testing_your_own_app():
    assert in_scope("http://localhost:3000/login", "http://localhost:3000/")
    assert not in_scope("http://127.0.0.1:3000/", "http://localhost:3000/")
