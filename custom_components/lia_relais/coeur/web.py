"""UN APPEL WEB PAR LIA CLOUD (15/11) — les trames de genre `WEB`, et ce que le module laisse passer.

Le même format que `TrameWeb.kt` (`relais-protocole`), octet pour octet :
`type (1) || numéro (4, gros-boutiste) || charge`.
- REQUETE (1) : `{"m":"GET","p":"/api/…","h":{…}}` en UTF-8, un octet nul, puis le corps ;
- ABANDON (2) : rien ; TETE (3) : `{"code":200,"h":{…}}` ; MORCEAU (4) : les octets bruts, 32 Ko
  au plus ; FIN (5) : rien ; ERREUR (6) : la raison en UTF-8.

Le module ne mène qu'à Home Assistant : même hôte, même port que sa WebSocket. Il refuse tout
chemin hors de `/api/` et `/auth/`, toute méthode hors de GET, POST, DELETE, et ne garde que trois
en-têtes de la demande (Authorization, Content-Type, Accept) et un de la réponse (Content-Type).
"""
import dataclasses
import json
from typing import Dict, Optional, Union
from urllib.parse import unquote, urlsplit, urlunsplit

REQUETE = 1
ABANDON = 2
TETE = 3
MORCEAU = 4
FIN = 5
ERREUR = 6

# Les octets d'un morceau de réponse, au plus : un morceau tient dans UNE trame chiffrée.
TAILLE_MORCEAU = 32 * 1024

METHODES = frozenset({"GET", "POST", "DELETE"})
PREFIXES = ("/api/", "/auth/")
# Les en-têtes de la demande transmis à HA, en minuscules ; les autres sont retirés sans bruit.
ENTETES_DEMANDE = {"authorization": "Authorization", "content-type": "Content-Type", "accept": "Accept"}


@dataclasses.dataclass(frozen=True)
class Requete:
    numero: int
    methode: str
    chemin: str
    entetes: Dict[str, str] = dataclasses.field(default_factory=dict)
    corps: bytes = b""


@dataclasses.dataclass(frozen=True)
class Abandon:
    numero: int


@dataclasses.dataclass(frozen=True)
class Tete:
    numero: int
    code: int
    entetes: Dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Morceau:
    numero: int
    octets: bytes


@dataclasses.dataclass(frozen=True)
class Fin:
    numero: int


@dataclasses.dataclass(frozen=True)
class Erreur:
    numero: int
    raison: str


Trame = Union[Requete, Abandon, Tete, Morceau, Fin, Erreur]


def _json(o) -> bytes:
    # Compact, clés dans l'ordre d'écriture, UTF-8 brut : ce qu'écrit kotlinx.serialization.
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encoder(t: Trame) -> bytes:
    if isinstance(t, Requete):
        type_, charge = REQUETE, _json({"m": t.methode, "p": t.chemin, "h": dict(t.entetes)}) + b"\x00" + t.corps
    elif isinstance(t, Abandon):
        type_, charge = ABANDON, b""
    elif isinstance(t, Tete):
        type_, charge = TETE, _json({"code": t.code, "h": dict(t.entetes)})
    elif isinstance(t, Morceau):
        type_, charge = MORCEAU, t.octets
    elif isinstance(t, Fin):
        type_, charge = FIN, b""
    elif isinstance(t, Erreur):
        type_, charge = ERREUR, t.raison.encode("utf-8")
    else:
        raise TypeError("trame web inconnue")
    return bytes([type_]) + (t.numero & 0xFFFFFFFF).to_bytes(4, "big") + charge


def _entetes(o) -> Dict[str, str]:
    h = o.get("h")
    if not isinstance(h, dict):
        return {}
    return {k: v for k, v in h.items() if isinstance(v, str)}


def lire(octets: bytes) -> Optional[Trame]:
    """La trame lue, ou None si elle est illisible."""
    if len(octets) < 5:
        return None
    type_, n, charge = octets[0], int.from_bytes(octets[1:5], "big"), octets[5:]
    try:
        if type_ == REQUETE:
            nul = charge.find(b"\x00")
            if nul < 0:
                return None
            o = json.loads(charge[:nul].decode("utf-8"))
            if not isinstance(o, dict) or not isinstance(o.get("m"), str) or not isinstance(o.get("p"), str):
                return None
            return Requete(n, o["m"], o["p"], _entetes(o), charge[nul + 1:])
        if type_ == ABANDON:
            return Abandon(n)
        if type_ == TETE:
            o = json.loads(charge.decode("utf-8"))
            if not isinstance(o, dict) or type(o.get("code")) is not int:
                return None
            return Tete(n, o["code"], _entetes(o))
        if type_ == MORCEAU:
            return Morceau(n, charge)
        if type_ == FIN:
            return Fin(n)
        if type_ == ERREUR:
            return Erreur(n, charge.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return None


def base_http(url_ha: str) -> str:
    """`ws://h:p/api/websocket` → `http://h:p` ; `wss` → `https` (http et https restent)."""
    u = urlsplit(url_ha)
    schema = {"ws": "http", "wss": "https"}.get(u.scheme, u.scheme)
    return urlunsplit((schema, u.netloc, "", "", ""))


def chemin_permis(chemin: str) -> bool:
    """Un chemin de HA, et rien d'autre : `/api/…` ou `/auth/…`, sans détour ni caractère de contrôle."""
    if not chemin.startswith(PREFIXES):
        return False
    if "//" in chemin or "://" in chemin or "\\" in chemin:
        return False
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in chemin):  # CR, LF, espaces, contrôles
        return False
    partie = chemin.split("?", 1)[0].split("#", 1)[0]
    # « .. » refusé aussi encodé (%2e%2e) : yarl le résoudrait.
    return all(unquote(s) not in ("..", ".") for s in partie.split("/")[1:])


def methode_permise(methode: str) -> Optional[str]:
    m = methode.upper()
    return m if m in METHODES else None


def entetes_demande(entetes: Dict[str, str]) -> Dict[str, str]:
    """Les seuls en-têtes transmis à HA ; une valeur à retour de ligne est retirée."""
    garde = {}
    for k, v in entetes.items():
        nom = ENTETES_DEMANDE.get(k.lower())
        if nom is not None and "\r" not in v and "\n" not in v:
            garde[nom] = v
    return garde


def entetes_reponse(entetes) -> Dict[str, str]:
    """Le seul en-tête rendu au téléphone : Content-Type. aiohttp décompresse déjà : jamais
    Content-Encoding ni Content-Length, qui mentiraient."""
    t = entetes.get("Content-Type")
    return {"Content-Type": t} if t else {}
