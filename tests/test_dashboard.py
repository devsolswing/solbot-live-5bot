"""Tests for the live-only, read-only dashboard (dashboard.py).

Covers: snapshot field completeness, P&L / balance / halt-distance
math, W/L recording, pipeline stage entries, secret scrubbing,
stale marking, and the HTTP server's read-only behaviour (GET
only; POST/PUT/DELETE/PATCH all refused; no trade/enable
endpoints). No network, no signing, no sending, no keys.
"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import (  # noqa: E402
    LiveDashboardState, build_snapshot, build_snapshot_empty,
    make_handler, pipeline_entries_for_candidate, render_html,
    scrub_public, serve_snapshot_callable, staleness,
)
from engine import Candidate, Portfolio, Position  # noqa: E402


def _portfolio():
    pf = Portfolio(cash_sol=10.0)
    pf.positions["MINTA"] = Position(
        mint="MINTA", entry_price_sol=0.01, invested_sol=2.0,
        tokens=200.0, peak_price_sol=0.012)
    return pf


class TestSnapshotFields(unittest.TestCase):
    def test_all_required_fields_present(self):
        snap = build_snapshot(
            _portfolio(), prices_sol={"MINTA": 0.012},
            sol_price_usd=100.0, sol_price_age_seconds=0.0,
            usd_per_cad=0.712, gate_status={"live_enabled": False},
            feed_status="ok", route_status="route_unverified")
        for key in ("dashboard", "currency", "wallet", "network",
                    "engine_state", "generated_at", "last_step_at",
                    "last_error", "gate_status", "feed_status",
                    "feed_age_seconds", "route_status", "balance",
                    "positions", "pnl", "pipeline", "passed_failed",
                    "win_loss", "recent_buys", "recent_sells"):
            self.assertIn(key, snap, key)
        self.assertEqual(snap["dashboard"].split("—")[0].strip(), "LIVE 5-bot engine")
        self.assertIn("NOT SolBot paper", snap["dashboard"])
        self.assertEqual(snap["currency"], "SOL only")
        for key in ("cash_sol", "positions_value_sol",
                    "portfolio_total_sol", "portfolio_total_cad",
                    "halt_threshold_cad", "halt_distance_cad", "halted"):
            self.assertIn(key, snap["balance"], key)
        for key in ("realized_pnl_sol", "unrealized_pnl_sol",
                    "total_pnl_sol", "per_coin_realized_pnl_sol"):
            self.assertIn(key, snap["pnl"], key)
        for key in ("wins", "losses", "win_rate_pct", "per_coin"):
            self.assertIn(key, snap["win_loss"], key)

    def test_position_and_balance_math(self):
        snap = build_snapshot(
            _portfolio(), prices_sol={"MINTA": 0.012},
            sol_price_usd=100.0, sol_price_age_seconds=0.0,
            usd_per_cad=0.712)
        pos = snap["positions"][0]
        self.assertAlmostEqual(pos["value_sol"], 2.4)
        self.assertAlmostEqual(pos["unrealized_pnl_sol"], 0.4)
        self.assertAlmostEqual(pos["gain_pct"], 20.0)
        self.assertAlmostEqual(snap["balance"]["positions_value_sol"], 2.4)
        self.assertAlmostEqual(snap["balance"]["portfolio_total_sol"], 12.4)
        # 12.4 SOL * $100 / 0.712 = CAD total; halt distance = total - 20.
        self.assertAlmostEqual(
            snap["balance"]["portfolio_total_cad"], 12.4 * 100.0 / 0.712)
        self.assertAlmostEqual(
            snap["balance"]["halt_distance_cad"],
            12.4 * 100.0 / 0.712 - 20.0)

    def test_stale_sol_price_gives_no_cad_total(self):
        snap = build_snapshot(
            _portfolio(), prices_sol={"MINTA": 0.012},
            sol_price_usd=100.0, sol_price_age_seconds=999.0,
            usd_per_cad=0.712)
        self.assertIsNone(snap["balance"]["portfolio_total_cad"])
        self.assertIsNone(snap["balance"]["halt_distance_cad"])

    def test_missing_mark_is_flagged_not_invented(self):
        snap = build_snapshot(_portfolio(), prices_sol={})
        pos = snap["positions"][0]
        self.assertIsNone(pos["current_price_sol"])
        self.assertTrue(pos["mark_stale_or_missing"])
        # Value falls back to entry (engine fail-closed rule), P&L 0.
        self.assertAlmostEqual(pos["value_sol"], 2.0)
        self.assertAlmostEqual(pos["unrealized_pnl_sol"], 0.0)

    def test_empty_snapshot_invents_nothing(self):
        snap = build_snapshot_empty()
        self.assertEqual(snap["engine_state"], "no_data_yet")
        self.assertEqual(snap["positions"], [])
        self.assertIsNone(snap["balance"]["portfolio_total_sol"])
        self.assertIsNone(snap["pnl"]["realized_pnl_sol"])
        self.assertIsNone(snap["last_step_at"])


class TestWinLoss(unittest.TestCase):
    def test_win_loss_and_realized_pnl(self):
        state = LiveDashboardState()
        state.record_buy("MINTA", 2.0)
        state.record_sell("MINTA", 2.5)   # win +0.5
        state.record_buy("MINTB", 1.0)
        state.record_sell("MINTB", 0.4)   # loss -0.6
        state.record_buy("MINTC", 1.0)
        state.record_sell("MINTC", 1.0)   # break-even: neither
        snap = build_snapshot(Portfolio(cash_sol=0.0),
                              dashboard_state=state)
        self.assertEqual(snap["win_loss"]["wins"], 1)
        self.assertEqual(snap["win_loss"]["losses"], 1)
        self.assertAlmostEqual(snap["win_loss"]["win_rate_pct"], 50.0)
        self.assertAlmostEqual(snap["pnl"]["realized_pnl_sol"], -0.1)
        self.assertAlmostEqual(
            snap["win_loss"]["per_coin"]["MINTA"]["realized_pnl_sol"], 0.5)
        self.assertAlmostEqual(
            snap["win_loss"]["per_coin"]["MINTB"]["realized_pnl_sol"], -0.6)

    def test_sell_without_recorded_buy_invents_no_pnl(self):
        state = LiveDashboardState()
        state.record_sell("UNKNOWN", 5.0)
        snap = build_snapshot(Portfolio(cash_sol=0.0),
                              dashboard_state=state)
        self.assertEqual(snap["win_loss"]["wins"], 0)
        self.assertEqual(snap["win_loss"]["losses"], 0)
        self.assertAlmostEqual(snap["pnl"]["realized_pnl_sol"], 0.0)


class TestPipelineEntries(unittest.TestCase):
    def _cand(self, **kw):
        base = dict(mint="M", gain_30s=0.05, gained_again_5s=True,
                    still_gaining=True, liquidity_usd=20000.0,
                    buys=10.0, sells=5.0, price_sol=0.01,
                    price_age_seconds=0.0)
        base.update(kw)
        return Candidate(**base)

    def test_full_pass_all_stages(self):
        entries = pipeline_entries_for_candidate(self._cand())
        self.assertEqual([e["stage"] for e in entries],
                         ["screener", "analyst", "secondary",
                          "executor_risk"])
        self.assertTrue(all(e["passed"] for e in entries))

    def test_screener_fail_blocks_later_stages(self):
        entries = pipeline_entries_for_candidate(
            self._cand(gain_30s=0.01))
        self.assertFalse(entries[0]["passed"])
        self.assertEqual(entries[1]["reason"], "not_reached")
        self.assertEqual(entries[3]["reason"], "not_reached")

    def test_analyst_fail_blocks_secondary(self):
        entries = pipeline_entries_for_candidate(
            self._cand(gained_again_5s=False))
        self.assertTrue(entries[0]["passed"])
        self.assertFalse(entries[1]["passed"])
        self.assertEqual(entries[2]["reason"], "not_reached")

    def test_blocked_reason_appears_in_snapshot(self):
        snap = build_snapshot(Portfolio(cash_sol=1.0),
                              blocked=["MINTX:route_unverified"])
        self.assertIn("MINTX:route_unverified",
                      snap["passed_failed"]["failures"])
        self.assertTrue(any(e["mint"] == "MINTX"
                            for e in snap["pipeline"]))


class TestSecretScrubbing(unittest.TestCase):
    def test_forbidden_keys_dropped_at_all_depths(self):
        dirty = {
            "positions": [],
            "TRADING_PRIVATE_KEY": "should-never-appear",
            "nested": {"seed": "abc", "signature": "xyz",
                       "public": "kept"},
            "api_key": "nope",
        }
        clean = scrub_public(dirty)
        dumped = json.dumps(clean)
        self.assertNotIn("should-never-appear", dumped)
        self.assertNotIn("abc", dumped)
        self.assertNotIn("xyz", dumped)
        self.assertNotIn("nope", dumped)
        self.assertEqual(clean["nested"]["public"], "kept")

    def test_snapshot_never_carries_key_material(self):
        snap = build_snapshot(
            _portfolio(), gate_status={
                "live_enabled": False,
                "TRADING_PRIVATE_KEY": "leak-attempt"})
        dumped = json.dumps(snap)
        self.assertNotIn("leak-attempt", dumped)
        self.assertNotIn("TRADING_PRIVATE_KEY", dumped)


class TestStaleness(unittest.TestCase):
    def test_fresh_snapshot_not_stale(self):
        now = time.time()
        snap = {"last_step_at": now, "feed_age_seconds": 0.0}
        verdict = staleness(snap, now=now)
        self.assertFalse(verdict["stale"])

    def test_old_snapshot_is_stale(self):
        now = time.time()
        snap = {"last_step_at": now - 120.0, "feed_age_seconds": 0.0}
        verdict = staleness(snap, now=now)
        self.assertTrue(verdict["stale"])
        self.assertIn("snapshot_older_than_step_interval",
                      verdict["reasons"])

    def test_old_feed_is_stale(self):
        now = time.time()
        snap = {"last_step_at": now, "feed_age_seconds": 120.0}
        verdict = staleness(snap, now=now)
        self.assertTrue(verdict["stale"])
        self.assertIn("feed_older_than_30s", verdict["reasons"])

    def test_no_step_yet_is_stale(self):
        snap = build_snapshot_empty()
        verdict = staleness(snap)
        self.assertTrue(verdict["stale"])
        self.assertIn("no_step_yet", verdict["reasons"])


class TestHttpServerReadOnly(unittest.TestCase):
    def setUp(self):
        self.state = LiveDashboardState()
        self.server = serve_snapshot_callable(
            lambda: self.state.snapshot(), port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _request(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_html_page_is_live_labelled_and_read_only(self):
        status, body = self._request("/")
        self.assertEqual(status, 200)
        self.assertIn("LIVE 5-bot dashboard", body)
        self.assertIn("NOT SolBot paper", body)
        self.assertIn("Read-only", body)
        self.assertNotIn("TRADING_PRIVATE_KEY", body)

    def test_json_endpoint(self):
        status, body = self._request("/snapshot.json")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["engine_state"], "no_data_yet")
        self.assertEqual(data["positions"], [])

    def test_unknown_path_404(self):
        status, _ = self._request("/nope")
        self.assertEqual(status, 404)

    def test_post_put_delete_patch_all_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, _ = self._request("/", method=method)
            self.assertEqual(status, 405, method)

    def test_no_trade_or_enable_endpoints(self):
        for path in ("/buy", "/sell", "/trade", "/enable",
                     "/disable", "/snapshot.json/trade"):
            for method in ("GET", "POST"):
                status, _ = self._request(path, method=method)
                self.assertIn(status, (404, 405), (path, method))


if __name__ == "__main__":
    unittest.main()
