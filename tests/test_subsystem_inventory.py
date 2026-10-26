from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import generate_subsystem_inventory as inventory


def test_inventory_covers_registered_routes_tables_and_source_anchors() -> None:
    source = inventory.APP_SOURCE.read_text(encoding="utf-8")
    first = inventory.render(source)
    second = inventory.render(source)
    assert first == second
    assert f"{len(inventory.endpoints(source=source))} method/path registrations" in first
    assert "Authorization path" in first
    assert "Mounted static assets" in first
    assert "MCP playground route" in first
    assert "`tool_audit_events`" in first
    assert "`ctx_vault_fts`" in first
    assert "deny-by-default policy and privacy-preserving audit boundary" in first
    assert "bundled inbound stdio MCP" in first
    assert "DuckDuckGo" in first
    assert "No separate remote-search opt-in" in first
    assert "24 GiB total memory" in first
    for endpoint in inventory.endpoints(source=source):
        assert endpoint.handler in first
        assert endpoint.path in first
        assert f"app.py#L{endpoint.line}" in first
    for table in inventory.TABLES:
        assert f"`{table}`" in first


def test_inventory_rejects_new_unmapped_endpoint() -> None:
    source = inventory.APP_SOURCE.read_text(encoding="utf-8")
    route = SimpleNamespace(path="/api/unmapped", methods={"GET"}, endpoint=inventory.app.healthz)
    with pytest.raises(inventory.InventoryError, match="No subsystem mapping"):
        inventory.render(source, routes=[route])


def test_inventory_rejects_unmapped_or_missing_database_table() -> None:
    source = inventory.APP_SOURCE.read_text(encoding="utf-8")
    with pytest.raises(inventory.InventoryError, match=r"undocumented=\['unreviewed_table'\]"):
        inventory._table_lines(source + "\nCREATE TABLE IF NOT EXISTS unreviewed_table (id INTEGER);\n")

    without_runs = source.replace("CREATE TABLE IF NOT EXISTS runs (", "CREATE TABLE IF NOT EXISTS untracked_runs (")
    with pytest.raises(inventory.InventoryError, match="missing=.*runs"):
        inventory._table_lines(without_runs)


def test_inventory_reports_unparseable_source_as_failure() -> None:
    with pytest.raises(inventory.InventoryError, match="Cannot parse app.py"):
        inventory.render("def broken(:\n")
