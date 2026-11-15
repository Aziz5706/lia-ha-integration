"""Ce que l'entité « Lia — accès de dehors » montre à l'app, commun à l'add-on et à l'intégration.

- `cle_publique` et `empreinte` : la clé PUBLIQUE du module, que l'app lit chez HA et épingle ;
- `appels_web` : ce module porte les appels web par Lia Cloud (lot G, 15/11). Un module plus ancien
  n'a pas cet attribut : l'app le lit comme « à mettre à jour » ;
- `version` : la version de l'add-on ou de l'intégration qui tourne.
"""
import hashlib

# Ce cœur relaie les appels web (`web.py`) : un module bâti sur lui le dit sur son entité.
APPELS_WEB = True


def attributs_du_module(publique: bytes, version: str) -> dict:
    return {
        "cle_publique": publique.hex(),
        "empreinte": hashlib.sha256(publique).hexdigest(),
        "appels_web": APPELS_WEB,
        "version": version,
    }
