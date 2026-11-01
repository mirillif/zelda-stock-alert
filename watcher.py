#!/usr/bin/env python3
"""Zelda Stock Monitor — moteur de surveillance.

Lit sites.json, vérifie chaque fiche produit, met à jour data/status.json et
data/history.json, et envoie un e-mail quand une boutique devient commandable.

Statuts possibles par boutique :
  available    bouton de commande actif + prix sous le plafond (alerte)
  over_price   commandable mais au-dessus du prix maximal (pas d'alerte)
  unavailable  rupture / indisponible
  unknown      vérification impossible (blocage anti-robot, page introuvable…)

Principes :
  - aucune alerte sans bouton de commande actif sur la fiche elle-même ;
  - toute détection positive est confirmée par une 2e lecture de la page ;
  - un statut « unknown » ne réarme jamais l'alerte (un CAPTCHA ne doit pas
    provoquer une nouvelle alerte au retour) ;
  - une nouvelle alerte n'est possible qu'après une rupture constatée.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import html as html_module
import json
import os
from pathlib import Path
import re
import smtplib
import sys
import time
import unicodedata
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "sites.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
STATUS_PATH = DATA_DIR / "status.json"
HISTORY_PATH = DATA_DIR / "history.json"
HISTORY_MAX = 500
PARIS = ZoneInfo("Europe/Paris")

# Mentions qui suffisent à classer la fiche en rupture.
NEGATIVE_STRONG = (
    "produit indisponible", "actuellement indisponible",
    "indisponible en ligne", "indisponible a la vente",
    "stock en ligne epuise", "precommande epuisee",
    "rupture de stock", "stock epuise", "epuise en ligne",
    "plus disponible", "indisponible pour le moment",
    "temporairement en rupture", "article indisponible", "hors stock",
)
# Mentions ambiguës (ex. « retrait en magasin non disponible ») : elles ne
# comptent que si aucun bouton de commande actif n'est présent.
NEGATIVE_WEAK = (
    "non disponible", "livraison indisponible", "creer une alerte",
    "recevoir une alerte stock", "m'alerter", "etre alerte",
    "bientot dispo", "prochainement disponible",
)
POSITIVE_BUTTON = (
    "ajouter au panier", "ajouter au caddie", "commander",
    "precommander", "acheter maintenant", "acheter en ligne",
    "ajouter a mon panier", "je commande",
)
BLOCK_MARKERS = (
    "captcha-delivery.com", "datadome", "px-captcha", "perimeterx",
    "cf-chl-", "challenge-platform", "cf_chl_opt", "_incapsula_resource",
    "request unsuccessful. incapsula", "access denied", "acces refuse",
    "are you a robot", "etes-vous un robot", "verifiez que vous etes humain",
    "vous avez ete bloque", "you have been blocked", "bot detection",
    "saisissez les caracteres que vous voyez", "type the characters you see",
    "api-services-support@amazon.com",
)
# La fiche doit être celle de la console, pas d'un accessoire ou du jeu.
ACCESSORY_WORDS = (
    "manette", "controller", "controleur", "pochette", "housse", "etui",
    "protection d'ecran", "protege", "amiibo", "sacoche", "carte cadeau",
    "support", "chargeur",
)
CONSOLE_HINTS = ("console", "anniversaire", "anniversary", "40e", "40eme", "40th", "edition limitee")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("’", "'")
    return " ".join(value.casefold().split())


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def paris_label(dt: datetime) -> str:
    return dt.astimezone(PARIS).strftime("%d/%m/%Y à %H:%M:%S")


@dataclass(frozen=True)
class Result:
    status: str
    detail: str
    price: float | None = None


# ---------------------------------------------------------------- détection

def looks_blocked(raw_html: str) -> bool:
    head = normalized(raw_html[:60000])
    return any(marker in head for marker in BLOCK_MARKERS)


def is_console_title(title: str) -> bool:
    t = normalized(title)
    if "zelda" not in t:
        return False
    if not ("switch 2" in t or "switch2" in t):
        return False
    if any(word in t for word in ACCESSORY_WORDS):
        return False
    return any(hint in t for hint in CONSOLE_HINTS)


def parse_price(text: str) -> float | None:
    if text is None:
        return None
    text = str(text).replace(" ", " ").replace(" ", " ")
    m = re.search(r"(\d{2,4}(?:[ .]\d{3})*)(?:\s*[,.]\s*(\d{1,2}))?", text)
    if not m:
        return None
    try:
        euros = float(m.group(1).replace(" ", "").replace(".", ""))
        cents = (m.group(2) or "0").ljust(2, "0")
        value = euros + int(cents) / 100
    except ValueError:
        return None
    return value if 100 <= value <= 4000 else None


def extract_price(visible: str) -> float | None:
    pattern = (r"(?<!\d)(\d{2,4}(?:[\s  ]\d{3})*)(?:\s*[,\.]\s*(\d{2}))?"
               r"\s*€(?:\s*[,\.]?\s*(\d{2})(?!\d))?")
    for match in re.finditer(pattern, visible):
        try:
            euros = float(re.sub(r"[\s  ]", "", match.group(1)))
            decimals = match.group(2) or match.group(3) or "00"
            value = euros + int(decimals) / 100
        except ValueError:
            continue
        if 100 <= value <= 4000:
            return value
    return None


def jsonld_product(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None]:
    """(disponibilité, prix, nom) d'après le JSON-LD Product de la page."""
    offers: list[dict] = []
    names: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            typ = node.get("@type", "")
            types = typ if isinstance(typ, list) else [typ]
            if any(isinstance(t, str) and t.casefold() == "product" for t in types):
                if isinstance(node.get("name"), str):
                    names.append(node["name"])
                local = node.get("offers", [])
                local = local if isinstance(local, list) else [local]
                for offer in local:
                    if isinstance(offer, dict) and isinstance(offer.get("offers"), list):
                        offers.extend(o for o in offer["offers"] if isinstance(o, dict))
                    elif isinstance(offer, dict):
                        offers.append(offer)
            if "@graph" in node:
                walk(node["@graph"])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text() or "null"))
        except (TypeError, json.JSONDecodeError):
            continue
    statuses, prices = set(), []
    for offer in offers:
        av = normalized(str(offer.get("availability", "")))
        if any(k in av for k in ("outofstock", "soldout", "discontinued")):
            statuses.add("unavailable")
        elif any(k in av for k in ("instock", "preorder", "presale", "limitedavailability")):
            statuses.add("available")
        for key in ("price", "lowPrice"):
            p = parse_price(offer.get(key)) if offer.get(key) is not None else None
            if p:
                prices.append(p)
                break
    availability = "unavailable" if "unavailable" in statuses else ("available" if statuses else None)
    return availability, (min(prices) if prices else None), (names[0] if names else None)


def page_title(soup: BeautifulSoup) -> tuple[object, str]:
    heading = soup.find("h1")
    if heading and heading.get_text(strip=True):
        return heading, heading.get_text(" ", strip=True)
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return None, og["content"]
    return None, soup.title.get_text(" ", strip=True) if soup.title else ""


def apply_price_cap(result: Result, max_price: float | None) -> Result:
    if result.status == "available" and result.price is not None and max_price is not None \
            and result.price > max_price:
        return Result("over_price",
                      f"Commandable mais {result.price:.2f} € > plafond {max_price:.0f} €",
                      result.price)
    return result


def detect_amazon(soup: BeautifulSoup, max_price: float | None) -> Result:
    title_node = soup.select_one("#productTitle")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    if not title:
        return Result("unknown", "Fiche Amazon illisible (titre absent, probablement bloquée)")
    if not is_console_title(title):
        return Result("unknown", f"Le titre ne correspond pas à la console : « {title[:80]} »")
    price = None
    for sel in ("#corePrice_feature_div .a-offscreen", "#corePriceDisplay_desktop_feature_div .a-offscreen",
                "#apex_desktop .a-offscreen", "#price_inside_buybox", "#priceblock_ourprice"):
        node = soup.select_one(sel)
        if node and parse_price(node.get_text()):
            price = parse_price(node.get_text())
            break
    availability = normalized(soup.select_one("#availability").get_text(" ", strip=True)) \
        if soup.select_one("#availability") else ""
    if any(p in availability for p in ("indisponible", "pas disponible", "rupture")):
        return Result("unavailable", f"Amazon : {availability[:80]}", price)
    button = soup.select_one("#add-to-cart-button, #buy-now-button, input[name='submit.add-to-cart']")
    if button is None or button.has_attr("disabled"):
        return Result("unavailable", "Aucun bouton « Ajouter au panier » sur l'offre principale", price)
    if price is None:
        return Result("unknown", "Bouton présent mais prix illisible : vérification manuelle conseillée")
    return apply_price_cap(Result("available", "Bouton « Ajouter au panier » actif (Amazon)", price), max_price)


def detect(raw_html: str, max_price: float | None, *, url: str = "") -> Result:
    if looks_blocked(raw_html):
        return Result("unknown", "Bloqué par la protection anti-robot du site")
    soup = BeautifulSoup(raw_html, "html.parser")
    if "amazon." in urlparse(url).netloc:
        return detect_amazon(soup, max_price)

    ld_status, ld_price, ld_name = jsonld_product(soup)
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    heading, title = page_title(soup)
    if not title and ld_name:
        title = ld_name
    if not title:
        return Result("unknown", "Fiche produit illisible (page vide ou rendue en JavaScript)")
    if not is_console_title(title) and not (ld_name and is_console_title(ld_name)):
        return Result("unknown", f"La page ne correspond pas à la console : « {title[:80]} »")

    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    text = main.get_text(" ", strip=True)
    pos = max(0, normalized(text).find(normalized(title)[:40]))
    zone = text[pos: pos + 3500]  # zone d'achat, avant les recommandations
    nzone = normalized(zone)
    price = ld_price or extract_price(zone)

    for phrase in NEGATIVE_STRONG:
        if phrase in nzone:
            return Result("unavailable", f"Mention « {phrase} » sur la fiche", price)
    if ld_status == "unavailable":
        return Result("unavailable", "Offre déclarée épuisée dans les données produit", price)

    has_button = False
    for button in main.select("button, input[type='submit'], input[type='button'], a[role='button'], a.btn"):
        label = normalized(button.get_text(" ", strip=True) or button.get("value", "")
                           or button.get("aria-label", "") or button.get("title", ""))
        if not any(action in label for action in POSITIVE_BUTTON):
            continue
        classes = " ".join(button.get("class", [])).casefold()
        disabled = (button.has_attr("disabled") or button.get("aria-disabled") == "true"
                    or "disabled" in classes or "is-disabled" in classes)
        if disabled:
            continue
        # Le bouton doit appartenir à la fiche (pas à un carrousel « produits similaires »).
        if heading is not None and button.find_previous("h1") is not heading:
            continue
        has_button = True
        break

    if not has_button:
        for phrase in NEGATIVE_WEAK:
            if phrase in nzone:
                return Result("unavailable", f"Mention « {phrase} » et aucun bouton de commande", price)
    if has_button:
        if price is None:
            return Result("unknown", "Bouton de commande actif mais prix illisible : vérification manuelle conseillée")
        return apply_price_cap(Result("available", "Bouton de commande actif sur la fiche", price), max_price)
    if ld_status == "available":
        return Result("unknown", "Annoncé en stock dans les métadonnées, mais aucun bouton actif lisible", price)
    return Result("unknown", "Aucun indice fiable (page probablement rendue en JavaScript)", price)


# ---------------------------------------------------------------- réseau

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
    "Cache-Control": "no-cache",
})


def fetch(url: str, timeout: int = 20) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("URL HTTPS requise")
    response = SESSION.get(url, timeout=timeout, allow_redirects=True)
    if response.status_code in (403, 429, 503):
        raise PermissionError(f"Bloqué par le site (HTTP {response.status_code})")
    if response.status_code == 404:
        raise FileNotFoundError("Page introuvable (404) : vérifie l'URL")
    response.raise_for_status()
    return response.text


def check(store: dict, max_price: float | None) -> Result:
    url = (store.get("url") or "").strip()
    if not url:
        return Result("unknown", "Aucune URL renseignée")
    try:
        result = detect(fetch(url), max_price, url=url)
        if result.status == "available":
            time.sleep(4)
            confirm = detect(fetch(url), max_price, url=url)
            if confirm.status != "available":
                return Result("unknown", f"Détection positive non confirmée à la 2e lecture ({confirm.detail})",
                              result.price)
            return confirm
        return result
    except (PermissionError, FileNotFoundError) as exc:
        return Result("unknown", str(exc))
    except requests.Timeout:
        return Result("unknown", "Le site n'a pas répondu à temps")
    except requests.ConnectionError:
        return Result("unknown", "Connexion au site impossible (refusée ou coupée)")
    except (requests.RequestException, ValueError) as exc:
        return Result("unknown", f"Lecture impossible : {str(exc)[:120]}")


# ---------------------------------------------------------------- e-mail

def email_settings() -> dict:
    return {
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip(),
        "to": os.environ.get("ALERT_TO", "").strip() or os.environ.get("SMTP_USERNAME", "").strip(),
        "host": os.environ.get("SMTP_HOST", "") or "smtp.gmail.com",
        "port": int(os.environ.get("SMTP_PORT", "") or "465"),
    }


def email_configured() -> bool:
    s = email_settings()
    return bool(s["username"] and s["password"] and s["to"])


def send_email(subject: str, html_body: str, text_body: str) -> None:
    s = email_settings()
    if not email_configured():
        raise RuntimeError("E-mail non configuré : ajoute SMTP_USERNAME et SMTP_PASSWORD dans les secrets GitHub")
    msg = EmailMessage()
    msg["From"] = f"Zelda Stock Monitor <{s['username']}>"
    msg["To"] = ", ".join(a.strip() for a in s["to"].split(",") if a.strip())
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if s["port"] == 465:
        with smtplib.SMTP_SSL(s["host"], s["port"], timeout=25) as smtp:
            smtp.login(s["username"], s["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s["host"], s["port"], timeout=25) as smtp:
            smtp.starttls()
            smtp.login(s["username"], s["password"])
            smtp.send_message(msg)


def alert_email(items: list[tuple[dict, Result]], product: str, when: datetime,
                dashboard_url: str) -> tuple[str, str, str]:
    names = ", ".join(store["name"] for store, _ in items)
    subject = f"🟢 STOCK — Switch 2 Zelda commandable chez {names}"
    cards, lines = [], []
    for store, result in items:
        price = f"{result.price:.2f} €".replace(".", ",") if result.price is not None else "prix à vérifier"
        url = html_module.escape(store["url"], quote=True)
        cards.append(
            '<div style="border:1px solid #d9d4c7;border-radius:10px;padding:16px;margin:12px 0">'
            f'<div style="font-size:18px;font-weight:bold">{html_module.escape(store["name"])}</div>'
            f'<div style="font-size:22px;margin:6px 0">{price}</div>'
            f'<a href="{url}" style="display:inline-block;background:#1f6f43;color:#fff;'
            'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:bold">'
            'Ouvrir la page d\'achat</a></div>')
        lines.append(f"{store['name']} — {price}\n{store['url']}")
    footer_link = (f'<p><a href="{html_module.escape(dashboard_url, quote=True)}">Tableau de bord</a></p>'
                   if dashboard_url else "")
    html_body = (
        '<html><body style="font-family:Arial,sans-serif;max-width:600px;color:#1d1d1b">'
        f'<h2 style="margin-bottom:4px">La console est commandable</h2>'
        f'<p style="margin-top:0;color:#555">{html_module.escape(product)}</p>'
        + "".join(cards) +
        f'<p style="color:#555">Détecté le {paris_label(when)} (heure de Paris).</p>'
        '<p style="color:#555">Vérifie le vendeur, le prix final et la livraison avant de payer : '
        'les stocks partent souvent en quelques minutes.</p>'
        + footer_link + '</body></html>')
    text_body = (f"La console est commandable : {product}\n\n" + "\n\n".join(lines)
                 + f"\n\nDétecté le {paris_label(when)} (heure de Paris).\n"
                 "Vérifie le vendeur, le prix final et la livraison avant de payer.")
    return subject, html_body, text_body


# ---------------------------------------------------------------- données

def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def load_config() -> dict:
    config = load_json(CONFIG_PATH, None)
    if not config:
        raise ValueError(f"Fichier de configuration manquant ou invalide : {CONFIG_PATH.name}")
    config.setdefault("stores", [])
    return config


def empty_status() -> dict:
    return {"version": 2, "stores": {}, "system": {}, "email": {}}


def store_view(store: dict, entry: dict) -> dict:
    return {
        "id": store["id"], "name": store["name"], "url": store.get("url", ""),
        "enabled": store.get("enabled", True),
        "status": entry.get("status", "pending"), "detail": entry.get("detail", "Pas encore vérifié"),
        "price": entry.get("price"), "checked_at": entry.get("checked_at"),
        "since": entry.get("since"), "armed": entry.get("armed", True),
    }


def finalize(status: dict, config: dict, now: datetime) -> None:
    """Construit la vue publique lue par le tableau de bord."""
    status["product"] = config.get("product", "")
    status["max_price_eur"] = config.get("max_price_eur")
    status["interval_minutes"] = int(config.get("interval_minutes", 5))
    status["updated_at"] = iso(now)
    status["email"]["configured"] = email_configured()
    ids = [s["id"] for s in config["stores"]]
    status["view"] = [store_view(s, status["stores"].get(s["id"], {})) for s in config["stores"]]
    # On oublie les boutiques supprimées de la configuration.
    status["stores"] = {k: v for k, v in status["stores"].items() if k in ids}


def run(*, dry_run: bool = False, event: str = "manual", dashboard_url: str = "") -> int:
    config = load_config()
    status = load_json(STATUS_PATH, empty_status())
    for key in ("stores", "system", "email"):
        status.setdefault(key, {})
    history = load_json(HISTORY_PATH, [])
    now = now_utc()
    interval = max(5, int(config.get("interval_minutes", 5)))

    last_run = status["system"].get("last_run_at")
    if event == "schedule" and last_run:
        elapsed = now - datetime.fromisoformat(last_run)
        if elapsed < timedelta(minutes=interval) - timedelta(seconds=90):
            print(f"Fréquence réglée sur {interval} min : prochain contrôle plus tard "
                  f"(dernier il y a {int(elapsed.total_seconds() // 60)} min).")
            return 0

    max_price = config.get("max_price_eur")
    alerts: list[tuple[dict, Result]] = []
    unknown, total = [], 0

    for store in config["stores"]:
        if not store.get("enabled", True):
            continue
        total += 1
        result = check(store, max_price)
        checked = iso(now_utc())
        entry = status["stores"].setdefault(store["id"], {"armed": True})
        previous = entry.get("status")
        print(f"[{checked}] {store['name']}: {result.status} — {result.detail}"
              + (f" — {result.price:.2f} €" if result.price else ""), flush=True)

        if result.status != previous:
            history.insert(0, {
                "at": checked, "store_id": store["id"], "store": store["name"],
                "from": previous or "pending", "to": result.status,
                "price": result.price, "detail": result.detail,
            })
            entry["since"] = checked
        entry.update({"status": result.status, "detail": result.detail,
                      "price": result.price, "checked_at": checked})

        if result.status == "unknown":
            unknown.append(store["name"])
        elif result.status in ("unavailable", "over_price"):
            entry["armed"] = True  # rupture constatée : la prochaine dispo déclenchera une alerte
        elif result.status == "available" and entry.get("armed", True):
            alerts.append((store, result))

    if alerts:
        subject, html_body, text_body = alert_email(alerts, config.get("product", "Console Zelda"),
                                                    now, dashboard_url)
        if dry_run:
            print("\n[TEST] E-mail simulé :\n" + subject + "\n" + text_body)
        else:
            try:
                send_email(subject, html_body, text_body)
                status["email"].update({"last_sent_at": iso(now), "last_error": None})
                for store, _ in alerts:
                    status["stores"][store["id"]]["armed"] = False
                    history.insert(0, {"at": iso(now), "store_id": store["id"], "store": store["name"],
                                       "from": "available", "to": "alert_sent",
                                       "price": status["stores"][store["id"]].get("price"),
                                       "detail": "E-mail d'alerte envoyé"})
                print(f"E-mail envoyé pour {len(alerts)} boutique(s)")
            except (OSError, RuntimeError, smtplib.SMTPException) as exc:
                # L'alerte reste armée : on réessaiera au prochain passage.
                status["email"]["last_error"] = f"{iso(now)} — {str(exc)[:200]}"
                print(f"ÉCHEC de l'envoi de l'e-mail : {exc}", file=sys.stderr)

    # Alerte technique si presque toutes les boutiques sont illisibles (max 1 / 24 h).
    if total >= 3 and len(unknown) >= (total * 3 + 3) // 4:
        last_warn = status["system"].get("last_warning_at")
        if not last_warn or now - datetime.fromisoformat(last_warn) >= timedelta(hours=24):
            body = ("La surveillance n'arrive pas à lire la plupart des boutiques "
                    "(blocage anti-robot ou URL obsolète).\n\nBoutiques concernées : " + ", ".join(unknown))
            if not dry_run and email_configured():
                try:
                    send_email("⚠️ Zelda Stock Monitor — boutiques illisibles",
                               f"<p>{html_module.escape(body)}</p>", body)
                    status["system"]["last_warning_at"] = iso(now)
                except (OSError, smtplib.SMTPException) as exc:
                    print(f"Avertissement non envoyé : {exc}", file=sys.stderr)

    status["system"].update({"last_run_at": iso(now), "last_run_event": event,
                             "last_run_duration_s": int((now_utc() - now).total_seconds())})
    finalize(status, config, now)
    if not dry_run:
        save_json(STATUS_PATH, status)
        save_json(HISTORY_PATH, history[:HISTORY_MAX])
    print(f"Contrôle terminé : {total} boutique(s), {len(alerts)} alerte(s), {len(unknown)} illisible(s)")
    return 0


def run_test_email(dashboard_url: str = "") -> int:
    status = load_json(STATUS_PATH, empty_status())
    status.setdefault("email", {})
    now = now_utc()
    fake = Result("available", "test", 469.99)
    subject, html_body, text_body = alert_email(
        [({"name": "Boutique de test", "url": "https://example.com"}, fake)],
        "E-MAIL DE TEST — aucune console n'a été détectée", now, dashboard_url)
    try:
        send_email("✅ TEST — " + subject, html_body, text_body)
        status["email"].update({"last_test_at": iso(now), "last_test_ok": True, "last_error": None})
        print("E-mail de test envoyé")
        code = 0
    except (OSError, RuntimeError, smtplib.SMTPException) as exc:
        status["email"].update({"last_test_at": iso(now), "last_test_ok": False,
                                "last_error": f"{iso(now)} — {str(exc)[:200]}"})
        print(f"ÉCHEC de l'e-mail de test : {exc}", file=sys.stderr)
        code = 1
    try:
        finalize(status, load_config(), datetime.fromisoformat(status.get("updated_at", iso(now))))
    except ValueError:
        pass
    status["email"]["configured"] = email_configured()
    save_json(STATUS_PATH, status)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Zelda Stock Monitor")
    parser.add_argument("--test-email", action="store_true", help="Envoie un e-mail de test")
    parser.add_argument("--dry-run", action="store_true", help="Vérifie sans envoyer ni enregistrer")
    args = parser.parse_args()
    event = os.environ.get("EVENT_NAME", "manual")
    dashboard = os.environ.get("DASHBOARD_URL", "")
    if args.test_email:
        return run_test_email(dashboard)
    return run(dry_run=args.dry_run, event=event, dashboard_url=dashboard)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        sys.exit(1)
