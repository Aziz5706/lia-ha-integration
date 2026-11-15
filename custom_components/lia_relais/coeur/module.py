"""LE MODULE : la ligne de contrôle vers le relais, une ligne par session, le pont vers Home Assistant.

Le déroulé est celui de `Controle.kt` (lot 1) :
1. la ligne de contrôle `wss://<relais>/v1/maison`, avec le jeton de la maison, puis `bonjour` ;
2. à chaque `session`, une seconde ligne `/v1/maison/session/<id>` : le premier message binaire est
   le message 1 de la poignée de main ; la maison demande à son `Autorisation`, répond ;
3. acceptée, la session s'ouvre vers Home Assistant (`url_ha`, le WebSocket de HA) et relaie dans les
   deux sens : un message de HA = un message chiffré ;
4. la ligne de contrôle revient seule si elle tombe : 1 s, puis le double, `attente_max` au plus.

LA SESSION WEB (15/11, « tout doit marcher dehors ») : l'historique, les caméras, l'écriture des
scènes passent par l'API web de HA, pas par sa WebSocket. Le téléphone ouvre pour cela une session
à part, qu'il reconnaît à son PREMIER message, de genre `WEB` (`web.py`) :
- la WebSocket de HA, ouverte comme toujours à l'ouverture de la session (le téléphone de la liaison
  attend `auth_required` avant de parler, les anciennes apps aussi), est alors quittée sans avoir
  rien reçu du téléphone ; si HA ne répond pas, le module attend `DELAI_PREMIER_WEB` le premier
  message avant de fermer sur « ha injoignable » ;
- la session ne porte plus que des appels web, plusieurs à la fois (`MAX_APPELS_WEB` au plus),
  chacun par son numéro : une requête → une tête, des morceaux de 32 Ko (un flux MJPEG en est une
  suite sans fin), une fin — ou une erreur, qui ne dit que le nom de la faute. Un abandon coupe
  l'appel, et sa connexion à HA ; la session fermée les coupe tous ;
- seulement vers HA : le même hôte et le même port que sa WebSocket, sous `/api/` ou `/auth/`, en
  GET, POST ou DELETE, sans redirection suivie, avec trois en-têtes au plus (`web.py`) ;
- un appel web après un message pour HA, ou un message pour HA dans une session web, ferme la session.

Le module ne mène qu'à `url_ha` : jamais vers un autre appareil du réseau. Il ne garde aucune clé
de Home Assistant : c'est l'app qui envoie la sienne, dans la session. Il ne journalise jamais le
contenu d'une session, seulement ce qui s'y passe (ouverte, refusée, fermée ; pour un appel web :
ouvert, son code, fermé — jamais le chemin, les en-têtes ni le contenu).
"""
import asyncio
import logging
from typing import Dict, Optional, Set

import aiohttp
from aiohttp import WSMsgType
from yarl import URL

from . import controle, web
from .autorisation import Autorisation, ToutAccepter
from .cles import PaireDeCles
from .poignee import PoigneeMaison, Premier
from .protocole import MAX_MESSAGE, Genre
from .session import AuModule, Message, Session, Web

JOURNAL = logging.getLogger("lia_relais")

# Une trame tient en 65 535 octets ; un peu de marge pour l'enveloppe du WebSocket.
TAILLE_MAX_TRAME = 65535 + 64

# HA injoignable : le temps laissé au téléphone pour dire qu'il ouvre une session web.
DELAI_PREMIER_WEB = 1.0
# Les appels web d'une session, à la fois.
MAX_APPELS_WEB = 16
# Pas de limite totale (un flux de caméra dure) ; 10 s pour joindre HA, 60 s sans un octet.
DELAIS_WEB = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)


class Module:
    def __init__(self, *, relais: str, jeton: str, url_ha: str, cle: PaireDeCles,
                 autorisation: Autorisation = ToutAccepter(), attente_max: float = 60.0,
                 delai_poignee: float = 30.0, verifier_tls_ha: bool = True):
        # `verifier_tls_ha` : à False seulement quand HA sert en https sur 127.0.0.1 (lot 6) — son
        # certificat porte son nom de dehors, pas 127.0.0.1 ; la ligne ne quitte pas la machine.
        self._ssl_ha = None if verifier_tls_ha else False
        self._relais = relais.rstrip("/")
        self._jeton = jeton
        self._url_ha = url_ha
        self._cle = cle
        self._autorisation = autorisation
        self._attente_max = attente_max
        self._delai_poignee = delai_poignee
        self._connecte = False
        self._arret = asyncio.Event()
        self._sessions: Set[asyncio.Task] = set()
        self._http: Optional[aiohttp.ClientSession] = None
        self._ws_controle: Optional[aiohttp.ClientWebSocketResponse] = None

    @property
    def connecte(self) -> bool:
        """La ligne de contrôle est ouverte."""
        return self._connecte

    @property
    def cle_publique(self) -> bytes:
        """La clé publique de la maison : à montrer chez HA, pour que l'app l'épingle (jamais la privée)."""
        return self._cle.publique

    def _entetes(self):
        return {"Authorization": "Bearer " + self._jeton}

    async def tourner(self) -> None:
        """Tient la ligne de contrôle, jusqu'à `arreter`."""
        # Aucun cookie gardé : un cookie posé par HA pour un téléphone ne doit jamais partir avec
        # l'appel d'un autre.
        self._http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        attente = 1.0
        try:
            while not self._arret.is_set():
                ouverte = False
                try:
                    ouverte = await self._ligne_de_controle()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # le relais refuse, le réseau tombe : on réessaie, sans planter
                    JOURNAL.warning("ligne de contrôle fermée : %s", type(e).__name__)
                self._connecte = False
                if ouverte:
                    attente = 1.0
                if self._arret.is_set():
                    break
                try:
                    await asyncio.wait_for(self._arret.wait(), timeout=min(attente, self._attente_max))
                except asyncio.TimeoutError:
                    pass
                attente = min(attente * 2, self._attente_max)
        finally:
            self._connecte = False
            for t in list(self._sessions):
                t.cancel()
            await asyncio.gather(*self._sessions, return_exceptions=True)
            await self._http.close()

    async def arreter(self) -> None:
        self._arret.set()
        if self._ws_controle is not None:
            await self._ws_controle.close()

    async def _ligne_de_controle(self) -> bool:
        async with self._http.ws_connect(self._relais + controle.CHEMIN_MAISON, headers=self._entetes(),
                                         heartbeat=controle.PING_SECONDES, max_msg_size=64 * 1024) as ws:
            self._ws_controle = ws
            await ws.send_str(controle.encoder(controle.Bonjour()))
            self._connecte = True
            JOURNAL.info("ligne de contrôle ouverte")
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                m = controle.lire(msg.data)
                if isinstance(m, controle.DemandeDeSession):
                    t = asyncio.create_task(self._session(m.id))
                    self._sessions.add(t)
                    t.add_done_callback(self._sessions.discard)
                elif isinstance(m, controle.Fin):
                    JOURNAL.info("le relais ferme la ligne : %s", m.raison)
                    break
            return True

    async def _session(self, id_session: str) -> None:
        try:
            async with self._http.ws_connect(self._relais + controle.chemin_session(id_session),
                                             headers=self._entetes(), heartbeat=controle.PING_SECONDES,
                                             max_msg_size=TAILLE_MAX_TRAME) as ws:
                try:
                    msg = await asyncio.wait_for(ws.receive(), self._delai_poignee)
                except asyncio.TimeoutError:
                    JOURNAL.info("session %s : poignée de main trop lente", id_session)
                    return
                if msg.type != WSMsgType.BINARY:
                    return
                poignee = PoigneeMaison(self._cle)
                premier = poignee.lire_premier(msg.data)
                if premier is None:
                    JOURNAL.info("session %s : poignée de main refusée (rien ne s'accorde)", id_session)
                    return
                statut = await self._autorisation.decider(premier)
                message_2, session = poignee.repondre(statut)
                await ws.send_bytes(message_2)
                if session is None:
                    JOURNAL.info("session %s : téléphone refusé (%s)", id_session, statut.value)
                    return
                JOURNAL.info("session %s ouverte", id_session)
                await self._relayer(ws, session, premier, id_session)
                JOURNAL.info("session %s fermée", id_session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            JOURNAL.warning("session %s : %s", id_session, type(e).__name__)

    async def _relayer(self, ws, session: Session, premier: Premier, id_session: str = "?") -> None:
        verrou = asyncio.Lock()

        async def _envoi(genre: Genre, octets: bytes) -> None:
            # Les trames d'un message partent d'un bloc : deux messages ne s'entremêlent jamais,
            # sinon les compteurs arriveraient dans le désordre et la session tomberait.
            async with verrou:
                for trame in session.chiffrer(genre, octets):
                    await ws.send_bytes(trame)

        async def envoyer(genre: Genre, octets: bytes) -> None:
            # Protégé de l'annulation (un appel web abandonné) : une trame chiffrée mais pas partie
            # ferait sauter le compteur, et la session avec. Le message commencé finit de partir.
            envoi = asyncio.ensure_future(_envoi(genre, octets))
            envoi.add_done_callback(lambda f: f.cancelled() or f.exception())
            await asyncio.shield(envoi)

        try:
            options = {} if self._ssl_ha is None else {"ssl": self._ssl_ha}
            ha = await self._http.ws_connect(self._url_ha, max_msg_size=MAX_MESSAGE, **options)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            # HA ne répond pas : une session web l'apprendra appel par appel ; une session de
            # liaison, d'un coup.
            try:
                r = await asyncio.wait_for(self._premier(ws, session, premier, envoyer), DELAI_PREMIER_WEB)
            except asyncio.TimeoutError:
                r = None
            if isinstance(r, Web):
                await self._session_web(ws, session, premier, envoyer, r, id_session)
            else:
                await envoyer(Genre.FERMETURE, "ha injoignable".encode())
            return

        bascule = []  # le premier message du téléphone, s'il ouvre une session web

        async def vers_ha() -> None:
            transmis = False
            async for msg in ws:
                if msg.type != WSMsgType.BINARY:
                    return
                r = session.lire(msg.data)
                if r is None:
                    continue
                if isinstance(r, Message):
                    try:
                        texte = r.octets.decode("utf-8")
                    except UnicodeDecodeError:
                        await envoyer(Genre.FERMETURE, "message illisible".encode())
                        return
                    transmis = True
                    await ha.send_str(texte)
                elif isinstance(r, AuModule):
                    reponse = await self._autorisation.au_module(premier, r.octets)
                    if reponse is not None:
                        await envoyer(Genre.AU_MODULE, reponse)
                elif isinstance(r, Web) and not transmis:
                    bascule.append(r)  # session web : HA n'a rien reçu du téléphone, on la quitte
                    return
                else:  # Fermeture, trame Invalide, ou un appel web après la liaison : on ferme.
                    return

        async def depuis_ha() -> None:
            async for msg in ha:
                if msg.type == WSMsgType.TEXT:
                    await envoyer(Genre.FIN, msg.data.encode())
                elif msg.type == WSMsgType.BINARY:
                    await envoyer(Genre.FIN, msg.data)
                else:
                    break
            await envoyer(Genre.FERMETURE, "ha fermé".encode())

        async with ha:
            taches = {asyncio.create_task(vers_ha()), asyncio.create_task(depuis_ha())}
            try:
                await asyncio.wait(taches, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in taches:
                    t.cancel()
                await asyncio.gather(*taches, return_exceptions=True)
        if bascule:
            await self._session_web(ws, session, premier, envoyer, bascule[0], id_session)

    async def _prochain(self, ws, session: Session):
        """Le prochain message complet du téléphone ; None si la ligne se ferme."""
        while True:
            msg = await ws.receive()
            if msg.type != WSMsgType.BINARY:
                return None
            r = session.lire(msg.data)
            if r is not None:
                return r

    async def _premier(self, ws, session: Session, premier: Premier, envoyer):
        """Le premier message du téléphone hors « au module » (ceux-là reçoivent leur réponse)."""
        while True:
            r = await self._prochain(ws, session)
            if not isinstance(r, AuModule):
                return r
            reponse = await self._autorisation.au_module(premier, r.octets)
            if reponse is not None:
                await envoyer(Genre.AU_MODULE, reponse)

    async def _session_web(self, ws, session: Session, premier: Premier, envoyer, r, id_session: str) -> None:
        """Une session d'appels web : plusieurs à la fois, chacun par son numéro, jusqu'à la fermeture."""
        base = web.base_http(self._url_ha)
        hote = URL(base)
        options = {} if self._ssl_ha is None else {"ssl": self._ssl_ha}
        appels: Dict[int, asyncio.Task] = {}

        async def trame(t) -> None:
            await envoyer(Genre.WEB, web.encoder(t))

        async def appel(n: int, methode: str, url: URL, entetes, corps: bytes) -> None:
            JOURNAL.info("session %s : appel web ouvert", id_session)
            try:
                async with self._http.request(methode, url, headers=entetes, data=corps or None,
                                              allow_redirects=False, timeout=DELAIS_WEB, **options) as rep:
                    JOURNAL.info("session %s : appel web %d", id_session, rep.status)
                    await trame(web.Tete(n, rep.status, web.entetes_reponse(rep.headers)))
                    async for morceau in rep.content.iter_chunked(web.TAILLE_MORCEAU):
                        await trame(web.Morceau(n, morceau))
                await trame(web.Fin(n))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # jamais le chemin ni le contenu : le nom de la faute seul
                await trame(web.Erreur(n, type(e).__name__))
            finally:
                if appels.get(n) is asyncio.current_task():
                    del appels[n]
                JOURNAL.info("session %s : appel web fermé", id_session)

        async def demander(t: web.Requete) -> None:
            n = t.numero
            if len(appels) >= MAX_APPELS_WEB:
                await trame(web.Erreur(n, "trop d'appels"))
                return
            methode = web.methode_permise(t.methode)
            url = URL(base + t.chemin, encoded=True) if web.chemin_permis(t.chemin) else None
            if n in appels or methode is None or url is None or (url.host, url.port) != (hote.host, hote.port):
                await trame(web.Erreur(n, "refusé"))
                return
            appels[n] = asyncio.create_task(appel(n, methode, url, web.entetes_demande(t.entetes), t.corps))

        try:
            while True:
                if isinstance(r, Web):
                    t = web.lire(r.octets)
                    if isinstance(t, web.Requete):
                        await demander(t)
                    elif isinstance(t, web.Abandon):
                        tache = appels.pop(t.numero, None)
                        if tache is not None:
                            tache.cancel()
                    # Une trame illisible ou d'un autre type est ignorée : la session continue.
                elif isinstance(r, AuModule):
                    reponse = await self._autorisation.au_module(premier, r.octets)
                    if reponse is not None:
                        await envoyer(Genre.AU_MODULE, reponse)
                else:  # Fermeture, trame Invalide, un message pour HA, ou la ligne tombée : on ferme.
                    return
                r = await self._prochain(ws, session)
        finally:
            for tache in appels.values():
                tache.cancel()
            await asyncio.gather(*appels.values(), return_exceptions=True)
