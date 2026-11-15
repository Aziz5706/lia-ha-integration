# Lia — accès de dehors

Intégration Home Assistant de l'application **Lia Home** : elle relie votre Home Assistant au relais
de Lia, chiffré de bout en bout, pour piloter votre maison quand vous n'êtes pas chez vous.

Elle ne sert qu'avec l'application Lia et un compte Lia. L'application l'installe et la met à jour
seule quand elle le peut (HACS, ou la Marketplace de Home Assistant). Rien n'est à régler à la main.

## Mettre à jour à la main

Si l'application affiche « Module Lia à mettre à jour dans Home Assistant » :

- **Home Assistant 2026.11 ou plus récent** (la Marketplace) : Paramètres › Marketplace. Si
  « Lia — accès de dehors » n'y est pas, connectez d'abord un compte GitHub à la Marketplace, puis
  « Add from link » › `Aziz5706/lia-ha-integration`. Ouvrez « Lia — accès de dehors »,
  mettez à jour, puis redémarrez Home Assistant.
- **Avec HACS** (jusqu'à 2026.10) : HACS › ⋮ › Dépôts personnalisés › `Aziz5706/lia-ha-integration`,
  catégorie « Intégration » ; puis « Lia — accès de dehors » › Télécharger ; puis redémarrez Home
  Assistant.

Vos réglages et la clé de la maison restent : rien à refaire.

https://liahome.app

Licence MIT.
