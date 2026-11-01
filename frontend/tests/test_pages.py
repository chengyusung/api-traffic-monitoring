"""Run from frontend/: uv run python -m unittest tests.test_pages"""

import json
import os
import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

os.environ.setdefault("API_BASE_URL", "http://127.0.0.1:9/api/v1")
os.environ.setdefault("API_WS_URL", "ws://127.0.0.1:9/unused")

from lib import api  # noqa: E402

ADMIN = {"id": 1, "email": "admin@example.com", "role": "ADMIN", "is_active": True}
SYSTEM = {
    "readiness": {
        "state": "HEALTHY",
        "queued": 0,
        "in_flight": 3,
        "retry_pending": 0,
        "total_uncommitted": 3,
        "retry_count": 0,
        "oldest_pending_age_s": 1.5,
        "last_commit_at": None,
        "writer_state": "IDLE",
        "active_connections": 1,
    },
    "pool": {"status": "Pool size: 5 ...", "pool_size": 5, "max_overflow": 10},
    "generator": {
        "thresholds": {"auth": 80},
        "thresholds_loaded_at": "2026-11-01T00:00:00Z",
        "paused_s": 0.0,
        "stopped_by": None,
    },
    "table_counts": {"users": 1},
}
TREND = {
    "bucket_start": "2026-10-31T08:00:00Z",
    "count": 60,
    "sum": 3000,
    "avg_per_observation": 50.0,
    "min": 40,
    "max": 70,
    "anomaly_count": 1,
}
REFUSAL = "You cannot deactivate or demote yourself."


def page(name: str, user: dict[str, Any]) -> AppTest:
    app = AppTest.from_file(f"../pages/{name}", default_timeout=30)
    app.session_state["token"] = "token"
    app.session_state["user"] = user
    return app


def fake_api(test: unittest.TestCase, **fakes: mock.Mock) -> dict[str, mock.Mock]:
    patcher = mock.patch.multiple(api, **fakes)
    patcher.start()
    test.addCleanup(patcher.stop)
    st.cache_data.clear()
    return fakes


def receiver_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "ws-receiver" and t.is_alive()}


class LiveMonitorTest(unittest.TestCase):
    def test_reruns_keep_one_receiver_per_session(self) -> None:
        """testing.md WS-02: a rerun reuses the session's receiver instead of adding one."""
        before = receiver_threads()
        app = page("1_Live_Monitor.py", ADMIN)
        receivers = []
        for _ in range(5):
            app.run()
            self.assertFalse(app.exception)
            receivers.append(app.session_state["ws_receiver"])
        self.addCleanup(receivers[0].stop)

        self.assertTrue(all(r is receivers[0] for r in receivers))
        self.assertEqual(len(receiver_threads() - before), 1)


class AdminPageTest(unittest.TestCase):
    def fake_api(self, **overrides: Any) -> dict[str, mock.Mock]:
        return fake_api(
            self,
            **{
                "admin_system": mock.Mock(return_value=SYSTEM),
                "admin_users": mock.Mock(
                    return_value={"items": [ADMIN], "page": 1, "page_size": 100, "total": 1}
                ),
                "admin_update_user": mock.Mock(),
                "list_services": mock.Mock(return_value=[]),
                "admin_event_types": mock.Mock(return_value=["auth.login", "user.updated"]),
                "admin_logs": mock.Mock(return_value={"items": [], "total": 0}),
                "list_records": mock.Mock(return_value={"items": [], "total": 0}),
                **overrides,
            },
        )

    def test_other_roles_get_a_notice_and_no_admin_call(self) -> None:
        fakes = self.fake_api()
        app = page("4_Admin.py", {**ADMIN, "role": "USER"})

        app.run()

        self.assertFalse(app.exception)
        self.assertIn("只有 ADMIN", app.info[0].value)
        self.assertFalse(any(fake.called for fake in fakes.values()))

    def test_an_admin_sees_every_section(self) -> None:
        self.fake_api()
        app = page("4_Admin.py", ADMIN)

        app.run()

        self.assertFalse(app.exception)
        headers = [h.value for h in app.header]
        self.assertEqual(
            headers, ["系統狀態", "使用者", "Service threshold", "系統日誌", "歷史查詢"]
        )
        event_types = app.selectbox(key="logs-event-type").options
        self.assertEqual(event_types, ["全部", "auth.login", "user.updated"])

    def test_reruns_within_ten_seconds_reuse_the_system_status(self) -> None:
        fakes = self.fake_api()
        app = page("4_Admin.py", ADMIN)

        app.run()
        app.run()

        self.assertFalse(app.exception)
        self.assertEqual(fakes["admin_system"].call_count, 1)
        self.assertEqual(fakes["admin_users"].call_count, 2)

    def test_a_refused_change_shows_the_backend_message(self) -> None:
        refused = api.ApiError(422, "VALIDATION_ERROR", REFUSAL)
        fakes = self.fake_api(admin_update_user=mock.Mock(side_effect=refused))
        app = page("4_Admin.py", ADMIN)
        app.run()

        app.toggle[0].set_value(False)
        app.button[0].click()
        app.run()

        fakes["admin_update_user"].assert_called_once_with("token", 1, {"is_active": False})
        self.assertEqual([e.value for e in app.error], [REFUSAL])


class RecordsPageTest(unittest.TestCase):
    def fake_api(self) -> dict[str, mock.Mock]:
        return fake_api(
            self,
            list_services=mock.Mock(return_value=[]),
            list_records=mock.Mock(return_value={"items": [], "total": 0}),
        )

    def test_a_user_sees_the_create_edit_and_import_blocks(self) -> None:
        self.fake_api()
        app = page("2_Records.py", {**ADMIN, "role": "USER"})

        app.run()

        self.assertFalse(app.exception)
        self.assertEqual(
            [e.label for e in app.expander],
            ["新增 record", "編輯 / 刪除（本頁可修改 0 筆）", "匯入 CSV / JSON"],
        )

    def test_a_viewer_sees_no_write_block(self) -> None:
        self.fake_api()
        app = page("2_Records.py", {**ADMIN, "role": "VIEWER"})

        app.run()

        self.assertFalse(app.exception)
        self.assertEqual(len(app.expander), 0)
        self.assertIn("VIEWER 只能查詢", app.caption[-1].value)

    def test_the_list_call_carries_the_filters_sort_and_page(self) -> None:
        fakes = self.fake_api()
        app = page("2_Records.py", {**ADMIN, "role": "VIEWER"})
        app.run()

        app.selectbox(key="records-source").set_value("IMPORT")
        app.text_input(key="records-q").set_value("checkout")
        app.run()

        token, params = fakes["list_records"].call_args.args
        self.assertEqual(token, "token")
        self.assertEqual(
            params,
            {
                "service": None,
                "source": "IMPORT",
                "is_anomaly": None,
                "start": None,
                "end": None,
                "q": "checkout",
                "sort": "-observed_at",
                "page": 1,
                "page_size": 20,
            },
        )


class AnalyticsPageTest(unittest.TestCase):
    def fake_api(self, **overrides: Any) -> dict[str, mock.Mock]:
        return fake_api(
            self,
            **{
                "list_services": mock.Mock(return_value=[]),
                "analytics": mock.Mock(return_value=[]),
                "export_xlsx": mock.Mock(return_value=b"PK xlsx bytes"),
                **overrides,
            },
        )

    def test_the_default_window_is_the_last_24_hours(self) -> None:
        fakes = self.fake_api()
        app = page("3_Analytics.py", ADMIN)

        app.run()

        self.assertFalse(app.exception)
        kind, params = fakes["analytics"].call_args_list[0].args[1:]
        self.assertEqual(kind, "summary")
        start = datetime.fromisoformat(params["start"])
        self.assertAlmostEqual(
            start, datetime.now(UTC) - timedelta(hours=24), delta=timedelta(minutes=1)
        )

    def test_the_download_button_appears_once_the_export_returns_bytes(self) -> None:
        fakes = self.fake_api()
        app = page("3_Analytics.py", ADMIN)
        app.run()
        self.assertEqual(len(app.get("download_button")), 0)

        app.button[0].click()
        app.run()

        self.assertFalse(app.exception)
        fakes["export_xlsx"].assert_called_once()
        self.assertEqual(len(app.get("download_button")), 1)

    def test_the_trend_chart_keeps_its_time_axis_and_tooltip_in_utc(self) -> None:
        """A bucket the table shows as 08:00 UTC must not shift to the browser's zone."""
        self.fake_api(
            analytics=mock.Mock(
                side_effect=lambda token, kind, params: [TREND] if kind == "trends" else []
            )
        )
        app = page("3_Analytics.py", ADMIN)

        app.run()

        self.assertFalse(app.exception)
        spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
        self.assertEqual(spec["encoding"]["x"].get("scale"), {"type": "utc"})
        self.assertIn({"field": "bucket_start_utc", "type": "nominal"}, spec["encoding"]["tooltip"])


if __name__ == "__main__":
    unittest.main()
