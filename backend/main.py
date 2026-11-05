import os
import sqlite3
import math
import requests
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="FloodShield")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "floodshield.db"
load_dotenv()
alerted = set()  # hotspots already alerted, so we only send once


def send_telegram(text):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass  # never let an alert failure break the app


def check_alerts(hotspots):
    for h in hotspots:
        if h["risk_level"] == "Critical" and h["id"] not in alerted:
            alerted.add(h["id"])
            send_telegram(
                f"FLOODSHIELD ALERT: CRITICAL flood risk at {h['name']} "
                f"(score {h['risk_score']}). Reasons: {', '.join(h['reasons'])}. "
                f"Avoid this area."
            )
        elif h["risk_level"] != "Critical" and h["id"] in alerted:
            alerted.discard(h["id"])  # allow a new alert if it goes Critical again

# name, lat, lon, low_elevation (0-1), drain_weakness (0-1), past_flooding (0 or 1)
# Coordinates and ratings are approximate starter values: Person C should refine them.
SEED_HOTSPOTS = [
    ("Silk Board Junction", 12.9176, 77.6233, 0.8, 0.9, 1),
    ("Bellandur", 12.9304, 77.6784, 0.9, 0.8, 1),
    ("Hebbal Flyover", 13.0358, 77.5970, 0.7, 0.7, 1),
    ("Mahadevapura", 12.9915, 77.6958, 0.8, 0.8, 1),
    ("KR Puram", 13.0074, 77.6959, 0.7, 0.7, 1),
    ("Yelahanka", 13.1007, 77.5963, 0.6, 0.6, 1),
    ("Marathahalli", 12.9591, 77.6974, 0.7, 0.8, 1),
    ("Madiwala", 12.9226, 77.6174, 0.7, 0.8, 1),
    ("Koramangala", 12.9352, 77.6245, 0.6, 0.7, 1),
    ("Sarjapur Road", 12.9100, 77.6850, 0.8, 0.7, 0),
]


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hotspots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, lat REAL, lon REAL,
            low_elevation REAL, drain_weakness REAL, past_flooding INTEGER,
            water_level REAL DEFAULT 0,
            updated_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hotspot_id INTEGER, lat REAL, lon REAL,
            type TEXT, note TEXT, created_at TEXT
        )"""
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL)"
    )
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('rain_mm', 0)")
    if conn.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO hotspots (name, lat, lon, low_elevation, drain_weakness, past_flooding) "
            "VALUES (?,?,?,?,?,?)",
            SEED_HOTSPOTS,
        )
    conn.commit()
    conn.close()


init_db()


def distance_km(lat1, lon1, lat2, lon2):
    r = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_rain(conn):
    return conn.execute("SELECT value FROM settings WHERE key='rain_mm'").fetchone()[0]


def compute_risk(h, rain_mm, blocked_count):
    rain_score = min(rain_mm / 100, 1) * 100
    water_score = min(h["water_level"], 100)
    blocked_score = min(blocked_count / 3, 1) * 100
    elev_score = h["low_elevation"] * 100
    hist_score = h["past_flooding"] * 100

    score = (
        0.30 * rain_score
        + 0.25 * water_score
        + 0.20 * blocked_score
        + 0.15 * elev_score
        + 0.10 * hist_score
    )
    score = round(score)

    if score >= 70:
        level = "Critical"
    elif score >= 40:
        level = "Watch"
    else:
        level = "Safe"

    reasons = []
    if rain_mm >= 30:
        reasons.append(f"{rain_mm:.0f} mm rainfall")
    if h["water_level"] >= 50:
        reasons.append(f"drain water level at {h['water_level']:.0f}%")
    if blocked_count > 0:
        reasons.append(f"{blocked_count} blocked drain report(s) nearby")
    if h["low_elevation"] >= 0.7:
        reasons.append("low-lying area")
    if h["past_flooding"]:
        reasons.append("flooded before")
    if not reasons:
        reasons.append("no major risk factors right now")

    return score, level, reasons


def build_hotspot(conn, row, rain_mm):
    blocked = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE hotspot_id=? AND type='blocked_drain'",
        (row["id"],),
    ).fetchone()[0]
    score, level, reasons = compute_risk(row, rain_mm, blocked)
    return {
        "id": row["id"],
        "name": row["name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "water_level": row["water_level"],
        "blocked_reports": blocked,
        "risk_score": score,
        "risk_level": level,
        "reasons": reasons,
    }


class Report(BaseModel):
    lat: float
    lon: float
    type: str = "blocked_drain"  # blocked_drain or waterlogging
    note: Optional[str] = ""


class SensorReading(BaseModel):
    hotspot_id: int
    water_level: float  # 0-100 percent


class RainInput(BaseModel):
    rain_mm: float


@app.get("/")
def home():
    return {"message": "FloodShield backend is running"}


@app.get("/hotspots")
def list_hotspots():
    conn = get_db()
    rain = get_rain(conn)
    rows = conn.execute("SELECT * FROM hotspots").fetchall()
    result = [build_hotspot(conn, r, rain) for r in rows]
    conn.close()
    check_alerts(result)
    return {"rain_mm": rain, "hotspots": result}

@app.get("/hotspots/{hotspot_id}")
def one_hotspot(hotspot_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM hotspots WHERE id=?", (hotspot_id,)).fetchone()
    if row is None:
        conn.close()
        return {"error": "hotspot not found"}
    result = build_hotspot(conn, row, get_rain(conn))
    conn.close()
    return result


@app.post("/reports")
def add_report(r: Report):
    conn = get_db()
    rows = conn.execute("SELECT * FROM hotspots").fetchall()
    nearest, best = None, 999
    for h in rows:
        d = distance_km(r.lat, r.lon, h["lat"], h["lon"])
        if d < best:
            nearest, best = h, d
    hotspot_id = nearest["id"] if nearest is not None and best <= 2.0 else None
    conn.execute(
        "INSERT INTO reports (hotspot_id, lat, lon, type, note, created_at) VALUES (?,?,?,?,?,?)",
        (hotspot_id, r.lat, r.lon, r.type, r.note, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {
        "status": "saved",
        "linked_hotspot": nearest["name"] if hotspot_id else None,
    }


@app.get("/reports")
def list_reports():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/sensor")
def sensor(s: SensorReading):
    conn = get_db()
    conn.execute(
        "UPDATE hotspots SET water_level=?, updated_at=? WHERE id=?",
        (s.water_level, datetime.now().isoformat(), s.hotspot_id),
    )
    conn.commit()
    conn.close()
    return {"status": "updated"}


@app.get("/rain")
def read_rain():
    conn = get_db()
    rain = get_rain(conn)
    conn.close()
    return {"rain_mm": rain}


@app.post("/rain")
def set_rain(r: RainInput):
    conn = get_db()
    conn.execute("UPDATE settings SET value=? WHERE key='rain_mm'", (r.rain_mm,))
    conn.commit()
    conn.close()
    return {"rain_mm": r.rain_mm}

@app.post("/rain/live")
def live_rain():
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 12.9716,
                "longitude": 77.5946,
                "daily": "precipitation_sum",
                "forecast_days": 1,
                "timezone": "Asia/Kolkata",
            },
            timeout=8,
        )
        mm = resp.json()["daily"]["precipitation_sum"][0] or 0
    except Exception:
        return {"rain_mm": None, "error": "could not reach forecast service"}
    conn = get_db()
    conn.execute("UPDATE settings SET value=? WHERE key='rain_mm'", (mm,))
    conn.commit()
    conn.close()
    return {"rain_mm": mm, "source": "Open-Meteo forecast for today, Bengaluru"}


@app.post("/reset")
def reset_demo():
    conn = get_db()
    conn.execute("DELETE FROM reports")
    conn.execute("UPDATE hotspots SET water_level=0")
    conn.execute("UPDATE settings SET value=0 WHERE key='rain_mm'")
    conn.commit()
    conn.close()
    alerted.clear()
    return {"status": "demo reset"}