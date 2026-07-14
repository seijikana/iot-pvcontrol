"""history_store のユニットテスト（一時DBを使用・実機不要）"""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import history_store


class HistoryStoreTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_path = history_store.DB_PATH
        history_store.DB_PATH = os.path.join(self._tmpdir, "test_history.db")
        history_store.init_db()

    def tearDown(self):
        history_store.DB_PATH = self._orig_path
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------

    def _make_status(self, pv=50.0, chg=40.0, load_tr=4.0, bat_v=13.5, bat_temp=25.0,
                     bms1_pw=None, bms2_pw=None):
        bms = {}
        if bms1_pw is not None:
            bms["bms1"] = {"pack_w": bms1_pw}
        if bms2_pw is not None:
            bms["bms2"] = {"pack_w": bms2_pw}
        return {
            "pv_power": pv,
            "bat_power": chg,
            "load_power": load_tr,
            "bat_voltage": bat_v,
            "bat_temp": bat_temp,
            "bms": bms,
        }

    def test_record_and_query_minute(self):
        history_store.record(self._make_status())
        result = history_store.query("minute")
        self.assertEqual(len(result["points"]), 1)
        pt = result["points"][0]
        self.assertAlmostEqual(pt["pv"], 50.0, places=1)
        self.assertAlmostEqual(pt["bat_v"], 13.5, places=2)

    def test_record_bms_discharge(self):
        # bms1 charging (+W), bms2 discharging (-W)
        status = self._make_status(bms1_pw=10.0, bms2_pw=-5.0)
        history_store.record(status)
        result = history_store.query("minute")
        pt = result["points"][0]
        self.assertAlmostEqual(pt["load_bms"], 5.0, places=1)

    def test_idempotent_record(self):
        history_store.record(self._make_status(pv=10.0))
        history_store.record(self._make_status(pv=20.0))  # same minute → replace
        result = history_store.query("minute")
        self.assertEqual(len(result["points"]), 1)
        self.assertAlmostEqual(result["points"][0]["pv"], 20.0, places=1)

    def test_rollup_hour(self):
        now = datetime.now().replace(second=0, microsecond=0)
        prev_hour = now.replace(minute=0) - timedelta(hours=1)
        with history_store._lock:
            with history_store._conn() as c:
                for i in range(60):
                    ts = int((prev_hour + timedelta(minutes=i)).timestamp())
                    c.execute("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?)",
                              (ts, 60.0, 50.0, 4.0, 0.5, 13.4, 24.0))

        history_store.rollup_hour(int(prev_hour.timestamp()))

        result = history_store.query("hour")
        self.assertEqual(len(result["points"]), 1)
        pt = result["points"][0]
        # 60 rows × 60W / 60 = 60 Wh
        self.assertAlmostEqual(pt["pv"], 60.0, places=1)
        self.assertAlmostEqual(pt["bat_v"], 13.4, places=1)

    def test_rollup_day_and_week_month(self):
        yesterday = datetime.now() - timedelta(days=1)
        day_start = datetime(yesterday.year, yesterday.month, yesterday.day)
        day_ts = int(day_start.timestamp())

        with history_store._lock:
            with history_store._conn() as c:
                for h in range(24):
                    c.execute(
                        "INSERT OR REPLACE INTO rollup VALUES ('hour',?,?,?,?,?,?,?,?,?,?,?)",
                        (day_ts + h * 3600,
                         12.0, 10.0, 1.0, 0.5, 13.3, 13.1, 13.5, 23.0, 20.0, 26.0),
                    )

        history_store.rollup_day(yesterday)

        result = history_store.query("day")
        self.assertEqual(len(result["points"]), 1)
        pt = result["points"][0]
        self.assertAlmostEqual(pt["pv"], 12.0 * 24, places=0)  # sum of 24h

    def test_query_pagination(self):
        with history_store._lock:
            with history_store._conn() as c:
                for i in range(10):
                    c.execute("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?)",
                              (1000000 + i * 60, 5.0, 4.0, 1.0, 0.2, 13.0, 22.0))

        # first page (latest 5)
        r1 = history_store.query("minute", limit=5)
        self.assertEqual(len(r1["points"]), 5)
        self.assertTrue(r1["has_more"])
        self.assertEqual(r1["points"][-1]["t"], 1000000 + 9 * 60)

        # second page
        oldest_t = r1["points"][0]["t"]
        r2 = history_store.query("minute", before=oldest_t, limit=5)
        self.assertEqual(len(r2["points"]), 5)
        self.assertFalse(r2["has_more"])

    def test_prune_removes_old_rows(self):
        cutoff_age = history_store._MINUTE_KEEP_SEC + 120
        old_ts = int(time.time()) - cutoff_age
        with history_store._lock:
            with history_store._conn() as c:
                c.execute("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?)",
                          (old_ts, 1.0, 1.0, 1.0, 0.0, 13.0, 20.0))
                c.execute("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?)",
                          (int(time.time()), 1.0, 1.0, 1.0, 0.0, 13.0, 20.0))

        import config as cfg
        orig = cfg.TRACER_LOG_DIR
        cfg.TRACER_LOG_DIR = self._tmpdir
        try:
            history_store.daily_export_and_prune()
        finally:
            cfg.TRACER_LOG_DIR = orig

        result = history_store.query("minute", limit=100)
        for pt in result["points"]:
            self.assertGreater(pt["t"], old_ts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
