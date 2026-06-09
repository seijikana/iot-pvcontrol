#!/usr/bin/env python3
"""CarIoT メインループ (Phase ①: Tracer監視 + Flask WebUI)

systemd から起動する。SIGTERM/SIGINT で graceful shutdown。
"""
import csv
import logging
import os
import signal
import threading
import time
from datetime import datetime

import config
import settings_store
import tracer as tracer_module
import bms as bms_module
import webui
import wifi_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_shutdown = threading.Event()


def _handle_signal(*_):
    logger.info("Shutdown signal received")
    _shutdown.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# CSV ログ
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "timestamp", "pv_voltage", "pv_current", "pv_power",
    "bat_voltage", "bat_current", "bat_power", "bat_temp", "bat_soc",
    "load_voltage", "load_current", "load_power",
    "charge_status", "charge_stopped", "mock",
]


def write_csv(data: dict):
    os.makedirs(config.TRACER_LOG_DIR, exist_ok=True)
    fname = os.path.join(
        config.TRACER_LOG_DIR,
        "tracer_" + datetime.now().strftime("%Y%m%d") + ".csv",
    )
    is_new = not os.path.exists(fname)
    row = {k: data.get(k, "") for k in _CSV_FIELDS}
    row["timestamp"] = datetime.now().isoformat(timespec="seconds")
    with open(fname, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------------------
# 制御ループ（温度ヒステリシス）
# ---------------------------------------------------------------------------

def control_loop(tracer) -> dict:
    data = tracer.read_all()
    cfg = settings_store.get()

    temp = data.get("bat_temp")
    if temp is not None:
        if temp >= cfg["temp_high"] and not data.get("charge_stopped"):
            ok = tracer.stop_charging(cfg["boost_voltage_stop_v"])
            if ok:
                data["charge_stopped"] = True
            else:
                logger.error("stop_charging failed (Modbus write error)")
        elif temp <= cfg["temp_low"] and data.get("charge_stopped"):
            # BMS cell OV中は温度ベースの再開を行わない
            if not bms_module.is_ov_active():
                tracer.resume_charging(cfg["boost_voltage_normal_v"])
                data["charge_stopped"] = False
            else:
                logger.info("resume_charging skipped: BMS cell OV still active")

    data["bms"] = bms_module.get_status()
    webui.set_status(data)
    return data


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main():
    logger.info("=== CarIoT starting (Phase ②) ===")

    settings_store.load()
    tracer = tracer_module.create_tracer()

    # BMS cell OV → Tracer充電停止コールバック
    def on_bms_ov(bms_name: str, reason: str):
        logger.warning("BMS OV alert [%s]: %s → stopping Tracer charging", bms_name, reason)
        ok = tracer.stop_charging()
        if ok:
            logger.warning("Tracer charging stopped by BMS OV protection [%s]", bms_name)
        else:
            logger.error("Tracer stop_charging FAILED after BMS OV alert [%s]!", bms_name)

    bms_module.set_alert_callback(on_bms_ov)

    # BMS 常時監視スレッド
    bms_module.start_polling(_shutdown)
    logger.info("BMS persistent monitoring started")

    # Flask を daemon thread で起動
    # WiFi監視スレッド
    wifi_thread = threading.Thread(
        target=wifi_manager.monitor_loop,
        args=(_shutdown,),
        daemon=True,
        name="wifi-monitor",
    )
    wifi_thread.start()
    logger.info("WiFi monitor started (AP fallback after %ds)", config.AP_WAIT_SEC)

    flask_thread = threading.Thread(
        target=lambda: webui.app.run(
            host=config.WEBUI_HOST,
            port=config.WEBUI_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
        name="flask",
    )
    flask_thread.start()
    logger.info("Flask WebUI → http://%s:%d", config.WEBUI_HOST, config.WEBUI_PORT)

    # 初回読み取り（ダッシュボードが空白にならないように）
    try:
        data = control_loop(tracer)
        logger.info("Initial read OK: PV=%.1fW Bat=%.2fV/%.1f°C/%d%%",
                    data.get("pv_power", 0), data.get("bat_voltage", 0),
                    data.get("bat_temp", 0), data.get("bat_soc", 0))
    except Exception as e:
        logger.warning("Initial read failed: %s", e)

    next_poll = time.monotonic()
    next_log = time.monotonic()

    while not _shutdown.is_set():
        now = time.monotonic()

        if now >= next_poll:
            next_poll = now + config.TRACER_POLL_SEC
            try:
                data = control_loop(tracer)
                logger.debug("PV=%.1fW Bat=%.2fV/%.1f°C/%d%% [%s]%s",
                             data.get("pv_power", 0), data.get("bat_voltage", 0),
                             data.get("bat_temp", 0), data.get("bat_soc", 0),
                             data.get("charge_status", ""),
                             " MOCK" if data.get("mock") else "")
            except Exception as e:
                logger.error("Tracer read error: %s", e)

        if now >= next_log:
            next_log = now + config.DATA_COLLECT_SEC
            status = webui.get_status()
            if status:
                try:
                    write_csv(status)
                    logger.info("CSV logged: PV=%.1fW Bat=%.2fV/%.1f°C/%d%%",
                                status.get("pv_power", 0), status.get("bat_voltage", 0),
                                status.get("bat_temp", 0), status.get("bat_soc", 0))
                except Exception as e:
                    logger.error("CSV write error: %s", e)

        _shutdown.wait(timeout=1.0)

    logger.info("Shutting down...")
    tracer.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
