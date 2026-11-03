#!/usr/bin/env python3
"""Bot 1: Wunsch-Einstiege. Laeuft alle ~5 Minuten per GitHub Actions.

- holt neue Telegram-Nachrichten per getUpdates (kein Dauer-Zuhoeren noetig)
- verwaltet deine Level (/add, /list, /remove, /abstand)
- schickt EINE Nachricht, wenn Kurse in die Naehe deiner Level kommen
- deine Daten liegen im PRIVATEN Repo (Ordner "daten"), nicht in diesem Repo
Nur Python-Standardbibliothek, nichts zu installieren.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
DATEI = os.path.join(os.environ.get("DATEN_ORDNER", "daten"), "daten.json")
CG = "https://api.coingecko.com/api/v3"
KOPF = {"User-Agent": "wunsch-bot/1.0", "Accept": "application/json"}

FAELLT = {"fällt", "faellt", "fallt", "fall", "runter", "down", "unter"}
STEIGT = {"steigt", "steigen", "hoch", "up", "ueber", "über"}

HILFE = (
    "Befehle:\n"
    "/add BTC 85000 – Alarm setzen (Richtung wird automatisch erkannt)\n"
    "/add BTC 85000 fällt 1,5 – Kurs FÄLLT auf 85000, Abstand 1,5 %\n"
    "/add ETH 4000 steigt – Kurs STEIGT auf 4000\n"
    "/list – alle Alarme mit aktuellem Kurs\n"
    "/remove BTC – alle BTC-Alarme löschen\n"
    "/remove BTC 85000 – nur diesen Alarm löschen\n"
    "/abstand 2 – Standard-Abstand auf 2 % stellen\n"
    "Ein erledigter Alarm wird mit erneutem /add wieder scharf.\n"
    "Hinweis: Der Bot prüft ca. alle 5–15 Minuten, Antworten kommen mit Verzögerung."
)


# ---------- Hilfsfunktionen ----------
def http_json(url, daten=None, tries=3):
    """Holt JSON. Bei 429 / 5xx / Netzfehlern: warten und neu versuchen (Backoff)."""
    warte = 8
    for versuch in range(tries):
        try:
            body, kopf = None, dict(KOPF)
            if daten is not None:
                body = json.dumps(daten).encode()
                kopf["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=kopf)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and versuch < tries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(int(ra), 60) if ra and ra.isdigit() else warte)
                warte *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if versuch < tries - 1:
                time.sleep(warte)
                warte *= 2
                continue
            raise


def tg(methode, **params):
    res = http_json(f"https://api.telegram.org/bot{TOKEN}/{methode}", daten=params)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {methode} fehlgeschlagen")
    return res["result"]


def senden(text):
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=CHAT_ID, text=text[i:i + 3900],
           disable_web_page_preview=True)


def zahl(text):
    """'85000', '85.000', '0,25', '1.234,5' -> float. Wirft ValueError."""
    t = text.strip().replace("$", "").replace("%", "")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    return float(t)


def fmt(p):
    if p >= 1:
        s = f"{p:,.2f}"
        return "$" + (s[:-3] if s.endswith(".00") else s)
    return "$" + f"{p:.8f}".rstrip("0").rstrip(".")


def pct(x):
    return f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")


# ---------- Daten (liegen im privaten Repo) ----------
def laden():
    try:
        with open(DATEI, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        d = {}
    d.setdefault("offset", 0)
    d.setdefault("abstand", 1.0)
    d.setdefault("alarme", [])
    return d


def speichern(d):
    os.makedirs(os.path.dirname(DATEI), exist_ok=True)
    tmp = DATEI + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATEI)


# ---------- Kursquellen ----------
def finde_coin(symbol):
    q = urllib.parse.urlencode({"query": symbol})
    d = http_json(f"{CG}/search?{q}")
    treffer = [c for c in d.get("coins", [])
               if c.get("symbol", "").upper() == symbol.upper()]
    treffer.sort(key=lambda c: c.get("market_cap_rank") or 10 ** 9)
    return treffer[0] if treffer else None


def coingecko_preise(ids):
    q = urllib.parse.urlencode({"ids": ",".join(ids), "vs_currencies": "usd"})
    d = http_json(f"{CG}/simple/price?{q}")
    return {i: float(d[i]["usd"]) for i in ids if i in d and "usd" in d[i]}


def coinbase_preis(symbol):
    d = http_json(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot", tries=2)
    return float(d["data"]["amount"])


def hole_preise(alarme):
    """Alle Kurse in EINEM CoinGecko-Aufruf; was fehlt, kommt von Coinbase."""
    ids = sorted({a["id"] for a in alarme})
    preise = {}
    if not ids:
        return preise
    try:
        preise = coingecko_preise(ids)
    except Exception as e:
        print("CoinGecko-Fehler:", e)
    for a in alarme:
        if a["id"] not in preise:
            try:
                preise[a["id"]] = coinbase_preis(a["symbol"])
            except Exception as e:
                print(f"Coinbase-Fehler {a['symbol']}:", e)
    return preise


# ---------- Befehle ----------
def befehl_add(d, args):
    if len(args) < 2:
        return "Benutzung: /add BTC 85000 [fällt|steigt] [Abstand in %]"
    sym = args[0].upper().lstrip("$")
    try:
        level = zahl(args[1])
    except ValueError:
        return f"Das Level '{args[1]}' habe ich nicht verstanden. Beispiel: /add BTC 85000"
    if level <= 0:
        return "Das Level muss größer als 0 sein."
    richtung, abstand = None, None
    for t in args[2:]:
        tl = t.lower()
        if tl in FAELLT:
            richtung = "fällt"
        elif tl in STEIGT:
            richtung = "steigt"
        else:
            try:
                abstand = zahl(t)
            except ValueError:
                return f"'{t}' habe ich nicht verstanden. Erlaubt: fällt, steigt oder eine Zahl (Abstand in %)."
            if abstand <= 0 or abstand > 50:
                return "Der Abstand muss zwischen 0 und 50 % liegen."
    coin = finde_coin(sym)
    if not coin:
        return f"Ich finde keinen Coin mit dem Kürzel {sym}. Tippfehler?"
    try:
        preis = coingecko_preise([coin["id"]]).get(coin["id"])
    except Exception:
        preis = None
    if richtung is None:
        richtung = "fällt" if (preis is None or preis > level) else "steigt"
    d["alarme"] = [a for a in d["alarme"]
                   if not (a["symbol"] == sym and abs(a["level"] - level) < 1e-12)]
    d["alarme"].append({"symbol": sym, "id": coin["id"], "name": coin["name"],
                        "level": level, "richtung": richtung, "abstand": abstand,
                        "erledigt": False})
    ab = abstand if abstand else d["abstand"]
    text = (f"✅ {coin['name']} ({sym}): Alarm, wenn der Kurs auf {fmt(level)} "
            f"{'FÄLLT' if richtung == 'fällt' else 'STEIGT'} "
            f"(Meldung ab {pct(ab)} % Abstand).")
    if preis is not None:
        text += f"\nAktuell: {fmt(preis)}"
    return text


def befehl_list(d):
    if not d["alarme"]:
        return "Keine Alarme. Mit /add BTC 85000 legst du den ersten an."
    aktiv = [a for a in d["alarme"] if not a.get("erledigt")]
    preise = hole_preise(aktiv) if aktiv else {}
    zeilen = [f"📋 Deine Wunsch-Einstiege (Standard-Abstand {pct(d['abstand'])} %):"]
    for a in sorted(d["alarme"], key=lambda a: (a.get("erledigt", False), a["symbol"], a["level"])):
        pfeil = "↓" if a["richtung"] == "fällt" else "↑"
        z = f"• {a['symbol']} {pfeil} {fmt(a['level'])}"
        if a.get("abstand"):
            z += f" (Abstand {pct(a['abstand'])} %)"
        if a.get("erledigt"):
            z += " ✅ erledigt"
        else:
            p = preise.get(a["id"])
            if p is not None:
                z += f" – aktuell {fmt(p)} ({(p - a['level']) / a['level'] * 100:+.1f} %)"
        zeilen.append(z)
    return "\n".join(zeilen)


def befehl_remove(d, args):
    if not args:
        return "Benutzung: /remove BTC  oder  /remove BTC 85000"
    sym = args[0].upper().lstrip("$")
    level = None
    if len(args) > 1:
        try:
            level = zahl(args[1])
        except ValueError:
            return f"Das Level '{args[1]}' habe ich nicht verstanden."
    vorher = len(d["alarme"])
    d["alarme"] = [a for a in d["alarme"]
                   if not (a["symbol"] == sym and (level is None or abs(a["level"] - level) < 1e-12))]
    n = vorher - len(d["alarme"])
    return f"🗑 {n} Alarm(e) für {sym} gelöscht." if n else f"Für {sym} gibt es keinen passenden Alarm."


def befehl_abstand(d, args):
    try:
        wert = zahl(args[0])
        assert 0 < wert <= 50
    except (IndexError, ValueError, AssertionError):
        return "Benutzung: /abstand 1,5   (Zahl zwischen 0 und 50, in Prozent)"
    d["abstand"] = wert
    return f"✅ Standard-Abstand ist jetzt {pct(wert)} %."


def befehl(d, text):
    teile = text.split()
    cmd = teile[0].split("@")[0].lower()
    args = teile[1:]
    if cmd in ("/start", "/hilfe", "/help"):
        return "👋 Wunsch-Einstiege-Bot.\n\n" + HILFE
    if cmd == "/add":
        return befehl_add(d, args)
    if cmd == "/list":
        return befehl_list(d)
    if cmd == "/remove":
        return befehl_remove(d, args)
    if cmd == "/abstand":
        return befehl_abstand(d, args)
    return "Den Befehl kenne ich nicht. /hilfe zeigt alle Befehle."


def verarbeite_updates(d):
    updates = tg("getUpdates", offset=d["offset"], timeout=0, allowed_updates=["message"])
    for u in updates:
        d["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != CHAT_ID:
            continue  # fremde Chats werden ignoriert
        text = (m.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        try:
            antwort = befehl(d, text)
        except Exception as e:
            antwort = f"⚠️ Das hat nicht geklappt ({type(e).__name__}). Ich versuche es später nochmal, schick den Befehl sonst erneut."
        senden(antwort)
        speichern(d)
    speichern(d)


# ---------- Alarm-Prüfung ----------
def pruefe_alarme(d):
    aktiv = [a for a in d["alarme"] if not a.get("erledigt")]
    if not aktiv:
        return
    preise = hole_preise(aktiv)
    treffer = []
    for a in aktiv:
        p = preise.get(a["id"])
        if p is None:
            continue
        ab = a.get("abstand") or d["abstand"]
        if a["richtung"] == "fällt":
            hit = p <= a["level"] * (1 + ab / 100)
        else:
            hit = p >= a["level"] * (1 - ab / 100)
        if hit:
            treffer.append((a, p))
    if not treffer:
        return
    zeilen = ["🎯 Wunsch-Einstieg in Reichweite:"]
    for a, p in treffer:
        pfeil = "↓" if a["richtung"] == "fällt" else "↑"
        zeilen.append(f"• {a['symbol']} {pfeil} Level {fmt(a['level'])} – aktuell {fmt(p)} "
                      f"({(p - a['level']) / a['level'] * 100:+.1f} %)")
    zeilen.append("Diese Alarme sind jetzt erledigt (kein Spam).")
    senden("\n".join(zeilen))          # erst senden ...
    jetzt = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for a, _ in treffer:               # ... dann als erledigt markieren
        a["erledigt"] = True
        a["erledigt_am"] = jetzt
    speichern(d)


# ---------- Quellen-Test (manuell starten) ----------
def quellentest():
    def kraken():
        d = http_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", tries=1)
        return float(list(d["result"].values())[0]["c"][0])

    tests = [
        ("CoinGecko Suche", lambda: finde_coin("BTC")["id"]),
        ("CoinGecko Preis", lambda: coingecko_preise(["bitcoin"])["bitcoin"]),
        ("Coinbase Preis", lambda: coinbase_preis("BTC")),
        ("Kraken Preis", kraken),
        ("Binance.com (nur zur Info)", lambda: float(http_json(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", tries=1)["price"])),
        ("Privates Daten-Repo da", lambda: os.path.isdir(os.path.dirname(DATEI)) or 1 / 0),
    ]
    zeilen = ["🧪 Quellen-Test von GitHub-Servern:"]
    for name, f in tests:
        try:
            zeilen.append(f"✅ {name}: {f()}")
        except Exception as e:
            zeilen.append(f"❌ {name}: {type(e).__name__} {e}")
    text = "\n".join(zeilen)
    print(text)
    senden(text)


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID fehlen (als GitHub Secrets anlegen).")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        return quellentest()
    d = laden()
    verarbeite_updates(d)
    pruefe_alarme(d)
    speichern(d)


if __name__ == "__main__":
    main()
