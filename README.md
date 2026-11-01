# Zelda Stock Monitor

Surveille la disponibilité de la **Nintendo Switch 2 – The Legend of Zelda – Édition 40e anniversaire** chez plusieurs enseignes françaises et envoie un e-mail dès qu'une offre valide (bouton de commande actif + prix ≤ plafond) apparaît.

Tout tourne gratuitement sur GitHub, sans rien installer sur ton ordinateur :

- **GitHub Actions** vérifie les boutiques toutes les 5 minutes (GitHub peut parfois retarder un passage de quelques minutes).
- **GitHub Pages** héberge le tableau de bord : `https://mirillif.github.io/zelda-stock-alert/`
- Les résultats sont publiés sur la branche `data` (un seul commit, réécrit à chaque passage).

## Mise en route (une seule fois, ~10 min)

1. **Mot de passe d'application Gmail**
   Active la validation en 2 étapes sur ton compte Google, puis crée un mot de passe d'application : https://myaccount.google.com/apppasswords (16 caractères).

2. **Secrets GitHub** › *Settings › Secrets and variables › Actions › New repository secret*
   - `SMTP_USERNAME` : ton adresse Gmail
   - `SMTP_PASSWORD` : le mot de passe d'application
   L'adresse de destination se règle ensuite depuis le tableau de bord (par défaut : l'adresse Gmail ci-dessus).

3. **Activer le tableau de bord** › *Settings › Pages* › Source : *Deploy from a branch* › Branche `main`, dossier `/docs` › Save.

4. **Premier passage** › onglet *Actions* › *Zelda Stock Monitor* › *Run workflow*.
   Coche « Envoyer seulement un e-mail de test » pour vérifier l'e-mail, puis relance sans la case pour la première vraie vérification.

5. **Connexion au tableau de bord** › bouton « Se connecter » › suis les instructions pour créer un jeton GitHub limité à ce dépôt (permissions *Actions*, *Contents*, *Secrets* en lecture/écriture). Le jeton reste dans ton navigateur.

## Ce que fait le tableau de bord

- Consultation (publique, lecture seule) : statut de chaque boutique, prix détecté, heure du dernier contrôle, historique des changements, avertissement si la surveillance s'arrête.
- Une fois connecté : « Vérifier maintenant », « Tester l'e-mail », et réglage de l'adresse e-mail, de la fréquence, du prix maximal, des boutiques et de leurs URL.

## Statuts

| Statut | Signification |
|---|---|
| Disponible | Bouton de commande actif sur la fiche, confirmé par une 2e lecture, prix ≤ plafond → e-mail |
| Trop cher | Commandable mais au-dessus du prix maximal → pas d'e-mail |
| Rupture | Mention de rupture ou offre déclarée épuisée |
| Vérification impossible | Blocage anti-robot, page introuvable, page rendue en JavaScript… Le système ne devine jamais |

**Anti-doublons** : une boutique ne déclenche qu'un e-mail tant qu'elle reste disponible. Une nouvelle alerte n'est possible qu'après une rupture constatée. Un blocage temporaire ne réarme pas l'alerte.

Si presque toutes les boutiques deviennent illisibles, un e-mail d'avertissement part (au plus une fois par 24 h).

## Limites à connaître

- Plusieurs enseignes (Amazon, Fnac, Cdiscount, Leclerc…) protègent leurs pages contre les robots. Depuis les serveurs GitHub, certaines seront souvent en « Vérification impossible ». C'est affiché tel quel ; garde aussi les alertes officielles des enseignes.
- Le dépôt doit rester **public** : c'est ce qui rend les minutes GitHub Actions illimitées (un dépôt privé dépasserait le quota gratuit avec un contrôle toutes les 5 min). Aucun secret n'est exposé : identifiants e-mail et adresse de destination sont des *secrets* chiffrés, masqués dans les journaux publics.
- GitHub met en pause les workflows planifiés après 60 jours sans activité sur le dépôt ; les publications sur la branche `data` comptent normalement comme activité. Le tableau de bord signale toute interruption.

## Développement

```bash
pip install -r requirements.txt pytest
python -m pytest -q          # tests de détection hors ligne
python watcher.py --dry-run  # vérification réelle sans e-mail ni enregistrement
```

Le dossier `Zelda_Stock_Alert_v2/` est une ancienne copie, il n'est plus utilisé.
