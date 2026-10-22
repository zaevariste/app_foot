"""Serveur de pronostics : football-data.org -> moteur -> SQLite -> API JSON.

Une seule source de données (football-data.org, v4). Pour chaque championnat, un seul appel récupère
tous les matchs terminés : on en déduit les statistiques des équipes, les moyennes du championnat,
la forme récente ET les résultats à enregistrer. Un second appel récupère les prochains matchs.

Lancer :  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from engine import level, pick, predict

BASE = "https://api.football-data.org/v4"
TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "5daf43931d424b1d83fb105a78a46f12")       # <-- votre clé football-data.org
DB = os.environ.get("DB_PATH", "pronostics.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))
K = 3  # poids du « prior » : ramène vers la moyenne du championnat les équipes ayant peu de matchs


def _parse_leagues(raw):
    out = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            code, _, season = x.partition(":")
            out.append((code.upper(), int(season) if season else None))
    return out


# Championnats suivis automatiquement, ex. "PL,PD,SA" ou "PL:2025" (code[:année de début de saison])
LEAGUES = _parse_leagues(os.environ.get("LEAGUES", ""))


# ---------- Base de données ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS predictions(
              fixture_id INTEGER PRIMARY KEY, league TEXT, home TEXT, away TEXT, kickoff_ts INTEGER,
              lh REAL, la REAL, ph REAL, pd REAL, pa REAL, btts REAL, o25 REAL,
              pick_label TEXT, pick_p REAL, pick_set TEXT, level TEXT,
              result TEXT, settled_at INTEGER, created_at INTEGER)"""
        )


# ---------- football-data.org ----------
def fd_get(path, **params):
    if not TOKEN:
        raise HTTPException(500, "FOOTBALL_DATA_TOKEN n'est pas défini.")
    r = requests.get(BASE + path, headers={"X-Auth-Token": TOKEN}, params=params, timeout=30)
    if r.status_code == 429:
        raise HTTPException(429, "Limite de requêtes atteinte (10 par minute en offre gratuite) : réessayez dans une minute.")
    if r.status_code in (403, 404):
        raise HTTPException(r.status_code, "Accès refusé ou compétition inconnue : vérifiez le code et ce que couvre votre offre.")
    r.raise_for_status()
    return r.json()


def _ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def collect(code, season):
    """Matchs terminés -> moyennes du championnat, stats par équipe, liste des matchs."""
    params = {"status": "FINISHED"}
    if season:
        params["season"] = season
    data = fd_get(f"/competitions/{code}/matches", **params)
    ms = [m for m in data["matches"] if m["score"]["fullTime"]["home"] is not None]
    ms.sort(key=lambda m: m["utcDate"])
    teams, hg, ag = {}, 0, 0

    def t(team):
        return teams.setdefault(team["id"], {"name": team.get("shortName") or team["name"],
                                             "hf": 0, "ha": 0, "hn": 0, "af": 0, "aa": 0, "an": 0, "pts": []})

    for m in ms:
        gh, ga = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        hg, ag = hg + gh, ag + ga
        th, ta = t(m["homeTeam"]), t(m["awayTeam"])
        th["hf"] += gh; th["ha"] += ga; th["hn"] += 1
        ta["af"] += ga; ta["aa"] += gh; ta["an"] += 1
        th["pts"].append(3 if gh > ga else 1 if gh == ga else 0)
        ta["pts"].append(3 if ga > gh else 1 if gh == ga else 0)
    n = len(ms)
    lg = {"name": data.get("competition", {}).get("name", code),
          "avgH": hg / n if n else 1.5, "avgA": ag / n if n else 1.2}
    return lg, teams, ms


def team_row(team, teams, lg):
    s = teams.get(team["id"])
    name = team.get("shortName") or team["name"]
    if not s:  # aucune donnée : moyenne du championnat
        return {"name": name, "hs": lg["avgH"], "hc": lg["avgA"], "as": lg["avgA"], "ac": lg["avgH"], "form": 7.5}
    sh = lambda total, n, avg: (total + K * avg) / (n + K)
    last = s["pts"][-5:]
    return {
        "name": s["name"],
        "hs": sh(s["hf"], s["hn"], lg["avgH"]), "hc": sh(s["ha"], s["hn"], lg["avgA"]),
        "as": sh(s["af"], s["an"], lg["avgA"]), "ac": sh(s["aa"], s["an"], lg["avgH"]),
        "form": sum(last) * 5 / len(last) if last else 7.5,
    }


def upcoming(nxt):
    today = datetime.now(timezone.utc).date()
    return lambda code: sorted(
        fd_get(f"/competitions/{code}/matches", status="SCHEDULED,TIMED", dateFrom=today.isoformat(),
               dateTo=(today + timedelta(days=11)).isoformat())["matches"],
        key=lambda m: m["utcDate"])[:nxt]


# ---------- Logique métier ----------
def settle_from(finished):
    done, now = 0, int(time.time())
    with db() as c:
        for m in finished:
            gh, ga = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
            res = "H" if gh > ga else "D" if gh == ga else "A"
            done += c.execute("UPDATE predictions SET result=?, settled_at=? WHERE fixture_id=? AND result IS NULL",
                              (res, now, m["id"])).rowcount
    return done


def refresh(code, season=None, nxt=10):
    """Règle les pronostics terminés, puis calcule et enregistre ceux des prochains matchs."""
    lg, teams, finished = collect(code, season)
    settled = settle_from(finished)
    now, n = int(time.time()), 0
    for m in upcoming(nxt)(code):
        h, a = team_row(m["homeTeam"], teams, lg), team_row(m["awayTeam"], teams, lg)
        r = predict(h, a, lg)
        pk = pick(r, h["name"], a["name"])
        ts = _ts(m["utcDate"])
        with db() as c:
            # Un pronostic n'est jamais modifié après le coup d'envoi (intégrité du suivi).
            c.execute(
                """INSERT INTO predictions(fixture_id,league,home,away,kickoff_ts,lh,la,ph,pd,pa,btts,o25,
                     pick_label,pick_p,pick_set,level,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fixture_id) DO UPDATE SET
                     lh=excluded.lh, la=excluded.la, ph=excluded.ph, pd=excluded.pd, pa=excluded.pa,
                     btts=excluded.btts, o25=excluded.o25, pick_label=excluded.pick_label,
                     pick_p=excluded.pick_p, pick_set=excluded.pick_set, level=excluded.level
                   WHERE predictions.kickoff_ts > ? AND predictions.result IS NULL""",
                (m["id"], code, h["name"], a["name"], ts, r["lh"], r["la"], r["ph"], r["pd"], r["pa"],
                 r["btts"], r["o25"], pk["label"], pk["p"], json.dumps(pk["set"]), level(pk["p"]), now, now))
        n += 1
    return {"updated": n, "settled": settled}


def _row(r):
    d = dict(r)
    d["pick_set"] = json.loads(d["pick_set"])
    return d


# ---------- Application ----------
async def _loop():
    while True:
        for code, season in LEAGUES:
            try:
                await asyncio.to_thread(refresh, code, season)
            except Exception as e:
                print("mise à jour", code, ":", e)
            await asyncio.sleep(15)  # reste sous la limite de 10 requêtes/minute
        await asyncio.sleep(REFRESH_HOURS * 3600)


@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(_loop()) if LEAGUES and TOKEN else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Pronostic IA", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def admin(x_admin_token: str = Header(default="")):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Jeton administrateur invalide.")


@app.get("/api/health")
def health():
    return {"ok": True, "leagues": LEAGUES}


@app.post("/api/refresh", dependencies=[Depends(admin)])
def api_refresh(league: str, season: Optional[int] = None, next: int = 10):
    return refresh(league.upper(), season, next)


@app.get("/api/predictions")
def predictions(status: str = "pending", limit: int = 50):
    cond = {"pending": "result IS NULL", "settled": "result IS NOT NULL", "all": "1=1"}.get(status)
    if cond is None:
        raise HTTPException(400, "status doit être pending, settled ou all.")
    order = "kickoff_ts ASC" if status == "pending" else "kickoff_ts DESC"
    with db() as c:
        rows = c.execute(f"SELECT * FROM predictions WHERE {cond} ORDER BY {order} LIMIT ?", (min(limit, 200),)).fetchall()
    return [_row(r) for r in rows]


@app.get("/api/stats")
def stats():
    with db() as c:
        rows = [_row(r) for r in c.execute("SELECT * FROM predictions WHERE result IS NOT NULL")]

    def rate(rs):
        return {"count": len(rs), "hit_rate": round(sum(r["result"] in r["pick_set"] for r in rs) / len(rs), 4) if rs else None}

    brier = None
    if rows:  # score de Brier sur 1N2 : plus bas = mieux calibré
        brier = sum((r["ph"] - (r["result"] == "H")) ** 2 + (r["pd"] - (r["result"] == "D")) ** 2
                    + (r["pa"] - (r["result"] == "A")) ** 2 for r in rows) / len(rows)
    return {"overall": rate(rows), "brier": brier,
            "by_level": {lv: rate([r for r in rows if r["level"] == lv]) for lv in ("high", "mid", "low")}}


@app.get("/api/export", dependencies=[Depends(admin)])
def export(league: str, season: Optional[int] = None, next: int = 10):
    """JSON à coller dans « Importer de vrais matchs » du prototype web."""
    code = league.upper()
    lg, teams, _ = collect(code, season)
    return {"league": {"name": lg["name"], "avgH": lg["avgH"], "avgA": lg["avgA"]},
            "fixtures": [{"home": team_row(m["homeTeam"], teams, lg), "away": team_row(m["awayTeam"], teams, lg),
                          "time": m["utcDate"][:16].replace("T", " ")} for m in upcoming(next)(code)]}
