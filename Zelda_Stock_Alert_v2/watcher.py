#!/usr/bin/env python3
"""Veille de stock légère, sans contournement de CAPTCHA ni connexion aux boutiques.

Statuts: available, unavailable, unknown.
La détection est conservatrice : elle ne prétend pas valider un panier ou un paiement.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import sys
import time
import unicodedata
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sites.json"
STATE_PATH = ROOT / "state.json"

# Les formulations suivantes désignent l'indisponibilité de la fiche ciblée.
NEGATIVE = (
    "produit indisponible", "actuellement indisponible",
    "indisponible en ligne", "indisponible a la vente",
    "stock en ligne epuise", "precommande epuisee",
    "rupture de stock", "stock epuise", "epuise en ligne",
    "bientot dispo", "plus disponible", "creer une alerte",
    "recevoir une alerte stock", "retrait indisponible",
    "livraison indisponible", "non disponible", "hors stock",
    "prochainement", "indisponible pour le moment",
)
POSITIVE_BUTTON = (
    "ajouter au panier", "ajouter au caddie", "commander",
    "precommander", "acheter maintenant", "acheter en ligne",
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class Result:
    status: str
    detail: str
    price: float | None = None


def product_part(soup: BeautifulSoup):
    """Cherche la section d'achat du produit pour éviter les recommandations en bas de page."""
    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    heading = main.find("h1")
    if not heading:
        # Ne jamais interpréter un bouton d'une page catégorie comme celui du produit.
        return None, None, ""
    text = main.get_text(" ", strip=True)
    heading_text = heading.get_text(" ", strip=True)
    position = normalized(text).find(normalized(heading_text))
    # La zone d'achat se trouve généralement à proximité du h1, avant les recommandations.
    visible = text[max(0, position): max(0, position) + 3200]
    return main, heading, visible


def jsonld_availability(soup: BeautifulSoup) -> str | None:
    """Indice complémentaire : uniquement les offres liées au Product JSON-LD."""
    offers = []
    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            typ = node.get("@type", "")
            is_product = (isinstance(typ, str) and typ.casefold() == "product") or (
                isinstance(typ, list) and "Product" in typ
            )
            if is_product:
                local_offers = node.get("offers", [])
                offers.extend(local_offers if isinstance(local_offers, list) else [local_offers])
            # @graph et objets top-level imbriqués, sans parcourir les offres produit similaire.
            if "@graph" in node:
                walk(node["@graph"])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (TypeError, json.JSONDecodeError):
            continue
    statuses = set()
    for item in offers:
        if isinstance(item, dict):
            av = normalized(str(item.get("availability", "")))
            if "outofstock" in av or "soldout" in av or "discontinued" in av:
                statuses.add("unavailable")
            elif "instock" in av or "preorder" in av or "presale" in av:
                statuses.add("available")
    if "unavailable" in statuses:
        return "unavailable"
    return "available" if "available" in statuses else None


def extract_price(visible: str) -> float | None:
    # Le prix se trouve d'ordinaire après le nom du produit dans la zone d'achat.
    # Accepte « 469,99€ », « 469 € », « 469 € , 00 ».
    for match in re.finditer(r"(?<!\d)(\d{2,4}(?:[\s\u00a0]\d{3})*)(?:\s*[,\.]\s*(\d{2}))?\s*€(?:\s*[,\.]\s*(\d{2}))?", visible):
        try:
            euros = float(match.group(1).replace(" ", "").replace("\u00a0", ""))
            decimals = match.group(2) or match.group(3) or "00"
            value = euros + int(decimals) / 100
            if 100 <= value <= 4000:
                return value
        except ValueError:
            pass
    return None


def detect(html: str, max_price_eur: float | None, *, store_id: str = "") -> Result:
    soup = BeautifulSoup(html, "html.parser")
    # Pas de javascript/style/cookie popup dans le texte.
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    main, heading, visible = product_part(soup)
    if heading is None:
        return Result("unknown", "Fiche produit introuvable (titre H1 absent)")
    title = normalized(heading.get_text(" ", strip=True))
    if not ("zelda" in title and ("switch 2" in title or "nintendo" in title)):
        return Result("unknown", "Le titre de la page ne correspond pas à la console Zelda")
    nvisible = normalized(visible)
    price = extract_price(visible)
    # Les faux signaux « ajouter au panier » sur d'autres offres sont courants.
    # Une rupture sur l'offre principale est donc prioritaire.
    for phrase in NEGATIVE:
        if phrase in nvisible:
            return Result("unavailable", f"Mention détectée : {phrase}", price)
    if price is not None and max_price_eur is not None and price > max_price_eur:
        return Result("unavailable", f"Prix affiché ({price:.2f} €) supérieur au plafond ({max_price_eur:.2f} €)", price)

    # On considère uniquement les CTA dans la partie principale de la fiche.
    buttons = main.select("button, input[type='submit'], input[type='button'], a[role='button']")
    has_purchase_button = False
    for button in buttons:
        label = normalized(button.get_text(" ", strip=True) or button.get("value", "") or button.get("aria-label", ""))
        disabled = button.has_attr("disabled") or button.get("aria-disabled") == "true"
        if not disabled and any(action in label for action in POSITIVE_BUTTON):
            # Les comparateurs/produits similaires ne doivent pas déclencher d'alerte.
            prev_heading = button.find_previous("h1")
            if prev_heading is not None and prev_heading is heading:
                has_purchase_button = True
                break
    structured = jsonld_availability(BeautifulSoup(html, "html.parser"))
    if structured == "unavailable":
        return Result("unavailable", "Offre produit structurée déclarée épuisée", price)
    if has_purchase_button:
        return Result("available", "Bouton de commande actif détecté sur la fiche produit", price)
    if structured == "available":
        # Un balisage JSON-LD n'est pas toujours à jour : on exige aussi une action.
        return Result("unknown", "Disponibilité annoncée par métadonnées, sans bouton de commande détecté", price)
    return Result("unknown", "Aucun indice fiable de commande ou de rupture", price)


def fetch(url: str, timeout: int = 18) -> str:
    url_parsed = urlparse(url)
    if url_parsed.scheme != "https" or not url_parsed.netloc:
        raise ValueError("URL HTTPS requise")
    response = requests.get(
        url,
        timeout=timeout,
        allow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
        },
    )
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", "text/html"):
        raise ValueError("Réponse non HTML")
    return response.text


def check(store: dict, max_price: float | None) -> Result:
    try:
        html = fetch(store["url"])
        result = detect(html, max_price, store_id=store["id"])
        # Limite les fausses alertes liées au cache / aux pages transitoires.
        if result.status == "available":
            time.sleep(3)
            confirmation = detect(fetch(store["url"]), max_price, store_id=store["id"])
            if confirmation.status != "available":
                return Result("unknown", f"Première détection non confirmée ({confirmation.detail})")
        return result
    except (requests.RequestException, ValueError) as exc:
        return Result("unknown", f"Lecture impossible : {str(exc)[:140]}")


def send_email(subject: str, html_body: str, text_body: str) -> None:
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    to_email = os.environ.get("ALERT_TO", "").strip()
    if not all([username, password, to_email]):
        raise RuntimeError("Configurer SMTP_USERNAME, SMTP_PASSWORD et ALERT_TO dans les secrets GitHub")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def email_content(items: list[tuple[dict, Result]], product: str):
    import html as html_module
    rows = []
    text_rows = []
    for store, result in items:
        url = html_module.escape(store["url"], quote=True)
        name = html_module.escape(store["name"])
        price = f"{result.price:.2f} €" if result.price is not None else "Prix à vérifier"
        rows.append(f'<li><strong>{name}</strong> — {price} — <a href="{url}">Ouvrir la fiche produit</a></li>')
        text_rows.append(f'{store["name"]} — {price}\n{store["url"]}')
    html_body = (
        '<html><body style="font-family:Arial,sans-serif;max-width:640px">'
        '<h2>🎮 La console Zelda semble commandable !</h2>'
        f'<p>{html_module.escape(product)}</p><ul>{"".join(rows)}</ul>'
        '<p><b>Attention :</b> stock non garanti. Vérifie le bouton d’achat, '
        'le prix final, le vendeur et la livraison avant de payer.</p>'
        '</body></html>'
    )
    text_body = (
        f'La console Zelda semble commandable : {product}\n\n' +
        '\n\n'.join(text_rows) +
        '\n\nVérifie le stock, le vendeur et le prix final avant de payer.'
    )
    return html_body, text_body


def run(dry_run=False):
    config = load_json(CONFIG_PATH, None)
    if not config:
        raise ValueError("Fichier sites.json manquant")
    state = load_json(STATE_PATH, {"stores": {}})
    state.setdefault("stores", {})
    max_price = config.get("max_price_eur")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts = []
    changed = False
    total = 0
    unknown_stores = []
    for store in config.get("stores", []):
        if not store.get("enabled", True):
            continue
        total += 1
        result = check(store, max_price)
        if result.status == "unknown":
            unknown_stores.append(store["name"])
        print(f'[{now}] {store["name"]}: {result.status} — {result.detail}', flush=True)
        previous = state["stores"].get(store["id"], {}).get("status")
        if result.status == "unavailable" and previous != "unavailable":
            state["stores"][store["id"]] = {"status": "unavailable", "changed_at": now}
            changed = True
        if result.status == "available" and previous != "available":
            alerts.append((store, result))
        # unknown : on ne désarme pas une alerte en cas de CAPTCHA, blocage, panne.
    if alerts:
        html_body, text_body = email_content(alerts, config.get("product", "Console Zelda"))
        if dry_run:
            print("\n[TEST] Envoi simulé :\n" + text_body)
        else:
            send_email("🎮 STOCK DÉTECTÉ — Nintendo Switch 2 Zelda !", html_body, text_body)
            print(f"✅ Email envoyé pour {len(alerts)} enseigne(s)")
            for store, _ in alerts:
                state["stores"][store["id"]] = {"status": "available", "changed_at": now}
                changed = True
    # Avertissement si presque toutes les boutiques sont inaccessibles.
    # Ce cas arrive lorsque les sites bloquent les robots : ne pas laisser
    # croire que la surveillance fonctionne silencieusement.
    if total >= 3 and len(unknown_stores) >= (total * 3 + 3) // 4:
        system = state.setdefault("system", {})
        last = system.get("last_warning_at")
        last_time = datetime.fromisoformat(last) if last else None
        if last_time is None or datetime.now(timezone.utc) - last_time >= timedelta(hours=24):
            body = ("La surveillance ne peut pas lire la plupart des boutiques. "
                    "Vérifie les logs GitHub Actions et les URLs.\n\n"
                    "Sites non analysables : " + ", ".join(unknown_stores))
            if dry_run:
                print("[TEST] Avertissement technique simulé : " + body)
            else:
                send_email("⚠️ Surveillance Zelda — boutiques inaccessibles", f"<p>{body}</p>", body)
                system["last_warning_at"] = now
                changed = True
    if changed and not dry_run:
        save_json(STATE_PATH, state)
    print(f"Contrôle terminé : {len(alerts)} nouvelle(s) possibilité(s) de commande")


def main():
    parser = argparse.ArgumentParser(description="Alerte stock Nintendo Switch 2 édition Zelda")
    parser.add_argument("--test-email", action="store_true", help="Envoie un email de test sans vérifier les magasins")
    parser.add_argument("--dry-run", action="store_true", help="Teste sans envoyer d'email ni modifier l'état")
    args = parser.parse_args()
    if args.test_email:
        body = "Test de notification réussi : l'outil pourra t'envoyer une alerte de stock."
        send_email("✅ Test e-mail — Zelda Stock Alert", f"<p>{body}</p>", body)
        print("✅ Email de test envoyé")
    else:
        run(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        sys.exit(1)
