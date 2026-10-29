import pytest

from sql_analyst.guard import MAX_ROWS, SqlRejected, cap_rows, check_read_only

ALLOWED = [
    "SELECT COUNT(*) FROM Track",
    "select * from Artist where Name like '%Delete%';",
    "WITH t AS (SELECT GenreId, COUNT(*) n FROM Track GROUP BY 1) SELECT * FROM t ORDER BY n DESC",
    "SELECT 'a;b' AS semi, 'x -- y' AS dashes",
    "SELECT replace_count FROM (SELECT 1 AS replace_count)",  # substring of a forbidden word is fine
]

REJECTED = [
    ("DELETE FROM Track", "Only SELECT"),
    ("INSERT INTO Artist(Name) VALUES ('x')", "Only SELECT"),
    ("DROP TABLE Track", "Only SELECT"),
    ("SELECT 1; DROP TABLE Track", "one statement"),
    ("WITH x AS (SELECT 1) DELETE FROM Track", "DELETE"),
    ("SELECT * FROM Track -- hide", "comments"),
    ("SELECT /* hi */ 1", "comments"),
    ("PRAGMA writable_schema=1", "Only SELECT"),
    ("ATTACH DATABASE 'x.db' AS x", "Only SELECT"),
    ("SELECT load_extension('evil.so')", "LOAD_EXTENSION"),
    ("SELECT writefile('/tmp/x', 'y')", "WRITEFILE"),
    ("", "Empty"),
    ("SELECT " + "1," * 1500 + "1", "longer than"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_selects_pass(sql):
    assert check_read_only(sql) == sql.rstrip(";")


@pytest.mark.parametrize("sql,why", REJECTED)
def test_everything_else_is_rejected(sql, why):
    with pytest.raises(SqlRejected, match=why):
        check_read_only(sql)


def test_cap_rows_wraps_without_changing_the_query():
    capped = cap_rows("SELECT * FROM Track")
    assert capped.endswith(f"LIMIT {MAX_ROWS}") and "SELECT * FROM Track" in capped
