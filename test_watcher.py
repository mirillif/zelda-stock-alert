import unittest
from watcher import detect, extract_price


def page(main):
    return '<html><main><h1>Console Nintendo Switch 2 Édition The Legend of Zelda 40 ans</h1>' + main + '</main></html>'


class WatcherTests(unittest.TestCase):
    def test_fnac_marketplace_cannot_trigger(self):
        html = page('<p>489,99 €</p><p>Stock en ligne épuisé</p><p>Autres offres</p><button>Ajouter au panier</button>')
        self.assertEqual(detect(html, 600).status, 'unavailable')

    def test_carrefour(self):
        self.assertEqual(detect(page('<p>Produit indisponible</p>'), 600).status, 'unavailable')

    def test_leclerc_placeholder_button(self):
        self.assertEqual(detect(page('<p>Précommande épuisée</p><button>Ajouter au panier</button>'), 600).status, 'unavailable')

    def test_stock(self):
        result = detect(page('<p>499,99 €</p><button>Ajouter au panier</button>'), 600)
        self.assertEqual(result.status, 'available')
        self.assertEqual(result.price, 499.99)

    def test_expensive_marketplace(self):
        result = detect(page('<p>938,85 €</p><button>Ajouter au panier</button>'), 600)
        self.assertEqual(result.status, 'unavailable')

    def test_disabled_cta(self):
        self.assertEqual(detect(page('<button disabled>Ajouter au panier</button>'), 600).status, 'unknown')

    def test_wrong_product(self):
        self.assertEqual(detect('<main><h1>Jeu Mario Switch 2</h1><button>Ajouter au panier</button></main>', 600).status, 'unknown')

    def test_retail_incorrect_no_heading(self):
        self.assertEqual(detect('<h2>Console Zelda</h2><button>Ajouter au panier</button>', 600).status, 'unknown')

    def test_price_syntax(self):
        self.assertEqual(extract_price('469 € , 00'), 469.0)


if __name__ == '__main__':
    unittest.main()
