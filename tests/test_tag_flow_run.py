"""Tests for _tag_flow_run_with_issue_id helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import prefect.runtime

from po_formulas.software_dev import _tag_flow_run_with_issue_id


def _make_logger() -> MagicMock:
    return MagicMock()


def _mock_flow_run(
    fr_id: str | None = "abc123", tags: list[str] | None = None
) -> MagicMock:
    m = MagicMock()
    m.get_id.return_value = fr_id
    m.tags = tags if tags is not None else []
    return m


def _mock_client() -> MagicMock:
    c = MagicMock()
    c.__enter__ = lambda s: s
    c.__exit__ = MagicMock(return_value=False)
    return c


def test_stamps_tag_on_flow_run() -> None:
    client = _mock_client()
    with (
        patch.object(prefect.runtime, "flow_run", _mock_flow_run()),
        patch("prefect.client.orchestration.get_client", return_value=client),
    ):
        _tag_flow_run_with_issue_id("my-issue-1", _make_logger())

    client.update_flow_run.assert_called_once_with(
        "abc123", tags=["issue_id:my-issue-1"]
    )


def test_skips_when_no_flow_run_id() -> None:
    logger = _make_logger()
    with patch.object(prefect.runtime, "flow_run", _mock_flow_run(fr_id=None)):
        _tag_flow_run_with_issue_id("my-issue-1", logger)

    logger.warning.assert_not_called()


def test_no_duplicate_tag() -> None:
    client = _mock_client()
    with (
        patch.object(
            prefect.runtime,
            "flow_run",
            _mock_flow_run(tags=["issue_id:my-issue-1"]),
        ),
        patch("prefect.client.orchestration.get_client", return_value=client),
    ):
        _tag_flow_run_with_issue_id("my-issue-1", _make_logger())

    client.update_flow_run.assert_not_called()


def test_swallows_exception_with_warning() -> None:
    logger = _make_logger()
    with (
        patch.object(prefect.runtime, "flow_run", _mock_flow_run()),
        patch(
            "prefect.client.orchestration.get_client",
            side_effect=RuntimeError("boom"),
        ),
    ):
        _tag_flow_run_with_issue_id("my-issue-1", logger)

    logger.warning.assert_called_once()
    assert "issue_id tag failed" in logger.warning.call_args[0][0]


def test_preserves_existing_tags() -> None:
    client = _mock_client()
    with (
        patch.object(
            prefect.runtime,
            "flow_run",
            _mock_flow_run(tags=["existing-tag"]),
        ),
        patch("prefect.client.orchestration.get_client", return_value=client),
    ):
        _tag_flow_run_with_issue_id("my-issue-1", _make_logger())

    client.update_flow_run.assert_called_once_with(
        "abc123", tags=["existing-tag", "issue_id:my-issue-1"]
    )
