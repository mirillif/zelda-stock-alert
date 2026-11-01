"""Tests de détection hors ligne : python -m pytest -q"""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import watcher
from watcher import Result, detect

TITLE = "Console Nintendo Switch 2 Édition 40e anniversaire The Legend of Zelda"


def page(body: str, title: str = TITLE, ld: dict | None = None) -> str:
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
    return f"<html><head>{script}</head><body><main><h1>{title}</h1>{body}</main></body></html>"


def test_available_with_active_button():
    r = detect(page('<span>469,99 €</span><button>Ajouter au panier</button>'), 600)
    assert r.status == "available" and r.price == 469.99


def test_disabled_button_is_not_available():
    r = detect(page('<span>469,99 €</span><button disabled>Ajouter au panier</button>'), 600)
    assert r.status != "available"


def test_strong_negative_wins_over_button():
    r = detect(page('<p>Rupture de stock</p><span>469 €</span><button>Ajouter au panier</button>'), 600)
    assert r.status == "unavailable"


def test_weak_negative_does_not_hide_real_stock():
    html = page('<span>469,99 €</span><p>Retrait en magasin non disponible</p>'
                '<button>Ajouter au panier</button>')
    assert detect(html, 600).status == "available"


def test_weak_negative_without_button():
    assert detect(page('<p>Créer une alerte</p><span>469 €</span>'), 600).status == "unavailable"


def test_over_price():
    r = detect(page('<span>899,00 €</span><button>Ajouter au panier</button>'), 600)
    assert r.status == "over_price" and r.price == 899.0


def test_button_in_recommendations_ignored():
    html = ("<html><body><main><h1>Autre produit</h1></main>"
            f"<section><h1>{TITLE}</h1></section></body></html>")
    assert detect(html, 600).status == "unknown"


def test_accessory_page_rejected():
    html = page('<span>89 €</span><button>Ajouter au panier</button>',
                title="Manette Pro Nintendo Switch 2 Édition 40e anniversaire Zelda")
    assert detect(html, 600).status == "unknown"


def test_game_page_rejected():
    html = page('<span>69 €</span><button>Ajouter au panier</button>',
                title="The Legend of Zelda: Ocarina of Time - Nintendo Switch 2")
    assert detect(html, 600).status == "unknown"


def test_captcha_is_unknown():
    html = '<html><body><iframe src="https://geo.captcha-delivery.com/x"></iframe></body></html>'
    r = detect(html, 600)
    assert r.status == "unknown" and "anti-robot" in r.detail


def test_jsonld_out_of_stock():
    ld = {"@type": "Product", "name": TITLE,
          "offers": {"@type": "Offer", "price": "469.99", "availability": "https://schema.org/OutOfStock"}}
    assert detect(page("<p>Fiche</p>", ld=ld), 600).status == "unavailable"


def test_jsonld_in_stock_without_button_is_unknown():
    ld = {"@type": "Product", "name": TITLE,
          "offers": {"@type": "Offer", "price": "469.99", "availability": "https://schema.org/InStock"}}
    assert detect(page("<p>Fiche</p>", ld=ld), 600).status == "unknown"


AMAZON = """<html><body><div id="dp"><span id="productTitle">{title}</span>
<div id="corePrice_feature_div"><span class="a-offscreen">469,99 €</span></div>
<div id="availability">{avail}</div>{button}</div></body></html>"""


def test_amazon_available():
    html = AMAZON.format(title=TITLE, avail="En stock", button='<input id="add-to-cart-button">')
    r = detect(html, 600, url="https://www.amazon.fr/dp/B0TEST")
    assert r.status == "available" and r.price == 469.99


def test_amazon_unavailable():
    html = AMAZON.format(title=TITLE, avail="Actuellement indisponible.", button="")
    assert detect(html, 600, url="https://www.amazon.fr/dp/B0TEST").status == "unavailable"


def test_amazon_robot_check():
    html = "<html><body>Saisissez les caractères que vous voyez ci-dessous</body></html>"
    assert detect(html, 600, url="https://www.amazon.fr/dp/B0TEST").status == "unknown"


def _run_with(results, tmp, status=None):
    cfg = {"product": "Zelda", "max_price_eur": 600, "interval_minutes": 5,
           "stores": [{"id": "a", "name": "A", "url": "https://a.fr/x", "enabled": True}]}
    (tmp / "sites.json").write_text(json.dumps(cfg))
    if status:
        (tmp / "data").mkdir(exist_ok=True)
        (tmp / "data" / "status.json").write_text(json.dumps(status))
    sent = []
    with mock.patch.object(watcher, "CONFIG_PATH", tmp / "sites.json"), \
         mock.patch.object(watcher, "STATUS_PATH", tmp / "data" / "status.json"), \
         mock.patch.object(watcher, "HISTORY_PATH", tmp / "data" / "history.json"), \
         mock.patch.object(watcher, "check", side_effect=results), \
         mock.patch.object(watcher, "send_email", side_effect=lambda *a: sent.append(a)), \
         mock.patch.dict(os.environ, {"SMTP_USERNAME": "x@gmail.com", "SMTP_PASSWORD": "p"}):
        watcher.run(event="manual")
    return sent, json.loads((tmp / "data" / "status.json").read_text())


def test_alert_once_then_rearm_after_rupture():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        av = Result("available", "ok", 469.0)
        sent, st = _run_with([av], tmp)
        assert len(sent) == 1
        sent, st = _run_with([av], tmp)          # toujours dispo : pas de doublon
        assert len(sent) == 0
        sent, st = _run_with([Result("unknown", "captcha")], tmp)
        sent, st = _run_with([av], tmp)          # un blocage ne réarme pas
        assert len(sent) == 0
        sent, st = _run_with([Result("unavailable", "rupture")], tmp)
        sent, st = _run_with([av], tmp)          # retour après rupture : nouvelle alerte
        assert len(sent) == 1
        history = json.loads((tmp / "data" / "history.json").read_text())
        assert any(h["to"] == "alert_sent" for h in history)
        assert st["view"][0]["status"] == "available"
