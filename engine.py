"""Moteur de pronostics : loi de Poisson + correction Dixon-Coles.
Identique au moteur du prototype web."""
import math

_FACT = [math.factorial(i) for i in range(9)]
RHO = -0.08


def _pois(k, lam):
    return math.exp(-lam) * lam**k / _FACT[k]


def _clamp(x, lo, hi):
    return min(hi, max(lo, x))


def predict(h, a, lg):
    """h, a : dict {name, hs, hc, as, ac, form} ; lg : dict {avgH, avgA}."""
    lh = h["hs"] * a["ac"] / lg["avgH"]
    la = a["as"] * h["hc"] / lg["avgA"]
    lh = _clamp(lh * (1 + (h["form"] - 7.5) / 7.5 * 0.10), 0.2, 4.5)
    la = _clamp(la * (1 + (a["form"] - 7.5) / 7.5 * 0.10), 0.2, 4.5)

    m, total = [], 0.0
    for i in range(9):
        row = []
        for j in range(9):
            t = 1.0
            if i == 0 and j == 0:
                t = 1 - lh * la * RHO
            elif i == 0 and j == 1:
                t = 1 + lh * RHO
            elif i == 1 and j == 0:
                t = 1 + la * RHO
            elif i == 1 and j == 1:
                t = 1 - RHO
            p = _pois(i, lh) * _pois(j, la) * t
            row.append(p)
            total += p
        m.append(row)

    ph = pd = pa = btts = o25 = 0.0
    for i in range(9):
        for j in range(9):
            p = m[i][j] / total
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
            if i > 0 and j > 0:
                btts += p
            if i + j > 2:
                o25 += p
    return {"lh": lh, "la": la, "ph": ph, "pd": pd, "pa": pa, "btts": btts, "o25": o25}


def pick(r, home, away):
    """Pronostic retenu : issue sèche si p >= 50 %, sinon double chance."""
    c = sorted(
        [(f"Victoire {home}", r["ph"], ["H"]), ("Match nul", r["pd"], ["D"]), (f"Victoire {away}", r["pa"], ["A"])],
        key=lambda x: -x[1],
    )[0]
    if c[1] >= 0.5:
        return {"label": c[0], "p": c[1], "set": c[2]}
    d1, d2 = r["ph"] + r["pd"], r["pd"] + r["pa"]
    if d1 >= d2:
        return {"label": f"{home} ou nul", "p": d1, "set": ["H", "D"]}
    return {"label": f"{away} ou nul", "p": d2, "set": ["D", "A"]}


def level(p):
    return "high" if p >= 0.65 else "mid" if p >= 0.5 else "low"
