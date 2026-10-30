# 🎮 Zelda Stock Alert — Nintendo Switch 2, édition 40e anniversaire

Un outil léger qui vérifie les pages de vente de la console Zelda en France et envoie un **e-mail dès qu'une page semble permettre de commander**. Il détecte un retour en stock après une rupture et évite les e-mails répétés si le stock reste affiché.

### Ce qui est préconfiguré

Carrefour, Fnac, E.Leclerc (deux versions), Cultura, Cdiscount, Auchan et Boulanger. Les liens pointent vers la console Zelda, pas vers une catégorie générique. La fiche Boulanger renvoyait une erreur 404 lors de sa vérification initiale : sa surveillance ne fonctionnera pas tant que la fiche n'est pas accessible.

Le prix maximal d'alerte est fixé à **600 €** pour éviter de recevoir des alertes concernant des revendeurs très chers. Ce filtre ne garantit pas l'identité du vendeur ; vérifie toujours le vendeur et le prix final.

## Installation sur GitHub (environ 10 minutes, une seule fois)

1. Télécharge puis décompresse l'archive ZIP. Crée un dépôt GitHub **public** nommé `zelda-stock-alert` (évite un dépôt privé si tu ne veux pas consommer les minutes GitHub Actions de ton forfait). Ne mets aucune adresse e-mail ni aucun mot de passe dans le dépôt, même public.
2. Avec **GitHub Desktop**, ajoute le dossier décompressé en tant que *Local repository* (option pour créer un dépôt si nécessaire), puis clique sur **Publish repository**. Conserve bien le dossier caché `.github` ; il contient la tâche automatique.
3. Sur Google, active la **validation en deux étapes** du compte Gmail qui enverra les alertes. Génère un **mot de passe d'application** dans les paramètres du compte Google : https://myaccount.google.com/apppasswords . Si cette option n'est pas disponible, utilise un compte compatible SMTP ou un service de mail transactionnel.
4. Sur le dépôt GitHub : **Settings → Secrets and variables → Actions → New repository secret**. Ajoute trois secrets :
   - `SMTP_USERNAME` : ton adresse Gmail complète (ex. `compte@gmail.com`)
   - `SMTP_PASSWORD` : le mot de passe d'application Gmail (16 caractères)
   - `ALERT_TO` : l'adresse e-mail qui doit recevoir les alertes (la même ou une autre)
5. Ouvre **Actions** → **🎮 Zelda — alerte stock** → **Run workflow**. Coche `test_email` puis lance : tu dois recevoir un mail « Test e-mail — Zelda Stock Alert ».
6. Lance ensuite une fois **Run workflow** sans cocher `test_email` pour vérifier la liste des magasins. Le cron s'exécutera ensuite automatiquement, normalement toutes les 5 minutes.

Tu peux également utiliser un serveur SMTP différent de Gmail : configure les variables de dépôt `SMTP_HOST` et `SMTP_PORT` si besoin, et renseigne les secrets SMTP habituels.

## Personnaliser les sites et le prix

Dans `sites.json` :
- Modifie `max_price_eur` (`600` par défaut ; `null` pour ne pas plafonner).
- Passe `"enabled": false` pour ignorer un magasin.
- Ajoute un magasin avec un identifiant unique, un nom et l'URL HTTPS de **sa fiche produit exacte**.

⚠️ Une nouvelle boutique peut utiliser une structure HTML différente : le moteur fonctionne mieux avec les enseignes déjà répertoriées, et il faudra éventuellement adapter ses règles de détection. Les offres *marketplace* ne sont pas vérifiées comme étant vendues directement par l'enseigne. Si un site bloque les robots (403, CAPTCHA, JavaScript obligatoire), le statut apparaîtra comme `unknown` : le script ne contourne pas ces protections.

## Tester sur ton ordinateur (sans envoi)

Avec Python 3.12+ :

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python watcher.py --dry-run
```

Pour un fonctionnement local permanent, configure ton planificateur système pour exécuter `python watcher.py` toutes les 5 minutes et renseigne les variables d'environnement SMTP. L'ordinateur doit rester allumé.

## Fonctionnement et limites

- Un passage vérifie chaque lien. Si une commande semble possible, le script vérifie une **deuxième fois**, environ 3 secondes après, avant d'envoyer l'e-mail.
- Les boutons d'achat doivent être actifs, et les indices de rupture sont prioritaires pour réduire les fausses alertes.
- `state.json` est automatiquement sauvegardé dans GitHub seulement lors d'un **changement d'état confirmé**, pour éviter les alertes répétées.
- En cas de panne réseau, page inaccessible, réponse inconnue ou CAPTCHA, il n'envoie pas de fausse alerte.
- Le contrôle toutes les cinq minutes n'est **pas une garantie de délai** : les actions programmées de GitHub peuvent commencer plus tard, et des stocks peuvent disparaître entre deux analyses.
- Aucun achat n'est effectué automatiquement. Une alerte indique une **possibilité** de commande, pas un panier ou une réservation confirmé.
- Les sites peuvent modifier leur code à tout moment et interrompre la détection. Consulte régulièrement l'onglet Actions pour voir les journaux et les erreurs.
- Vérifie les conditions des sites et respecte leurs éventuelles restrictions d'accès automatique.

**Arrêt immédiat :** dans GitHub → Actions → workflow → menu `…` → **Disable workflow**, ou désactive le dépôt.
