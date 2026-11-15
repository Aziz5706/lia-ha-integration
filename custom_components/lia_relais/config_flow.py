"""L'assistant de configuration, en deux étapes (lot 7a), et le changement de relais ou de jeton.

1. **user** : l'adresse du relais. Le module crée alors sa clé (une fois, dans `.storage`).
2. **jeton** : l'assistant MONTRE la clé publique du module et son empreinte (dans la réponse de
   l'assistant, `description_placeholders`), puis attend le jeton de la maison.
   Pourquoi cet ordre : notre cloud donne le jeton en inscrivant la maison, et pour l'inscrire il lui
   faut la clé du module. L'app lit donc la clé ici, chez HA, avec sa clé d'administrateur (c'est
   aussi la clé qu'elle épinglera), inscrit la maison (`enroler_maison_relais`), puis donne le jeton.
3. **reconfigure** : changer de relais ou de jeton sans retirer l'intégration ; la clé du module ne
   change pas, les téléphones qui l'ont épinglée la reconnaissent toujours.

Les règles sont dans le cœur (`coeur/reglages.py`), communes à l'add-on.
"""
import hashlib

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow

from .coeur.cles import charger_ou_creer
from .coeur.reglages import valider
from .const import CONF_JETON, CONF_RELAIS, DOMAIN, FICHIER_CLE

# Un jeton valable, pour ne valider que le relais à la première étape.
_JETON_NEUTRE = "x" * 16
# Une adresse valable, pour ne valider que le jeton à la seconde.
_RELAIS_NEUTRE = "wss://relais.liahome.app"


class LiaRelaisFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._relais = None
        self._cle = None

    async def _cle_du_module(self):
        """La clé du module : lue sur le disque, ou créée une fois (hors de la boucle de HA)."""
        return await self.hass.async_add_executor_job(charger_ou_creer, self.hass.config.path(".storage", FICHIER_CLE))

    async def async_step_user(self, user_input=None):
        erreurs = {}
        valeurs = user_input or {}
        if user_input is not None:
            relais = str(user_input.get(CONF_RELAIS, "")).strip().rstrip("/")
            erreurs = {k: v for k, v in valider(relais, _JETON_NEUTRE).items() if k == "relais"}
            if not erreurs:
                self._relais = relais
                self._cle = await self._cle_du_module()
                return await self.async_step_jeton()
        schema = vol.Schema({vol.Required(CONF_RELAIS, default=valeurs.get(CONF_RELAIS, "")): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=erreurs)

    async def async_step_jeton(self, user_input=None):
        erreurs = {}
        if user_input is not None:
            jeton = str(user_input.get(CONF_JETON, "")).strip()
            erreurs = {k: v for k, v in valider(_RELAIS_NEUTRE, jeton).items() if k == "jeton"}
            if not erreurs:
                return self.async_create_entry(title="Lia", data={CONF_RELAIS: self._relais, CONF_JETON: jeton})
        return self.async_show_form(
            step_id="jeton",
            data_schema=vol.Schema({vol.Required(CONF_JETON): str}),
            errors=erreurs,
            description_placeholders={
                "cle_publique": self._cle.publique.hex(),
                "empreinte": hashlib.sha256(self._cle.publique).hexdigest(),
            },
        )

    async def async_step_reconfigure(self, user_input=None):
        entree = self._get_reconfigure_entry()
        erreurs = {}
        if user_input is not None:
            relais = str(user_input.get(CONF_RELAIS, "")).strip().rstrip("/")
            jeton = str(user_input.get(CONF_JETON, "")).strip()
            erreurs = valider(relais, jeton)
            if not erreurs:
                return self.async_update_reload_and_abort(entree, data={CONF_RELAIS: relais, CONF_JETON: jeton})
        schema = vol.Schema({
            vol.Required(CONF_RELAIS, default=entree.data.get(CONF_RELAIS, "")): str,
            vol.Required(CONF_JETON): str,
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=erreurs)
