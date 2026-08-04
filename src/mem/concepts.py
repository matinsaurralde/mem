"""The concept map: what a person *says*, mapped to what they *typed*.

Shell history is written in one vocabulary and remembered in another. Nobody
types ``openssl x509 -in cert.pem -noout -dates`` and later recalls it as
"openssl x509"; they recall it as *"the command I used to check the
certificate"*. Substring search cannot bridge those two vocabularies, and
measured on ten such questions against a real history it finds nothing at all
— recall@5 of 0/10.

The bridge is this module: a curated dictionary from a natural-language
concept to the shell vocabulary that expresses it. It is ~200 lines of JSON
that anyone can read, grep, edit, translate and send as a pull request.

Why a dictionary rather than a model, in one paragraph: the alternatives were
measured. Asking the on-device Apple Foundation Model to expand the query
scored 2/10 at 2086 ms per query and hallucinated ``certutil /rebuild`` — a
*Windows* tool — for the certificate question. Apple's ``NLContextualEmbedding``
scored 0/5 on top-1, because a natural-language BERT has never seen
``lsof -i :8080`` and shell commands are simply out of its distribution. The
dictionary scores 8/10 in 3.9 ms: four times the accuracy, five hundred times
the speed, and unlike either model it can be *fixed* when it is wrong. See
``docs/decisions/011-concept-map-over-embeddings.md``.

Everything here is standard library only and free of Pydantic, for the same
reason :mod:`mem.ranking` is: the interactive finder's entire first-frame
budget is smaller than Pydantic's import.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

# The shipped map lives beside this module and is read through
# ``importlib.resources``, exactly like ``mem/hooks/mem.zsh`` — the one
# resolution that works identically for an editable checkout, a wheel and a
# zipimport. A previous version of the hook loader walked ``__file__`` upwards
# instead, and shipped a stale copy to every pip user for four months.
CONCEPTS_FILENAME = "concepts.json"

# A user map lives here. Not a *replacement* for the shipped file — a layer
# over it, so an upgrade still delivers new concepts to someone who added one.
USER_CONCEPTS_FILENAME = "concepts.json"

# Keys starting with an underscore are metadata, not concepts. JSON has no
# comment syntax, and a map meant to be read and edited by hand needs one.
_RESERVED_PREFIX = "_"
_STOPWORDS_KEY = "_stopwords"

# The longest concept key, in words. Multi-word keys ("disk space", "pull
# request") are matched against consecutive query words, longest first, so
# "disk space" never decays into "disk" AND "space".
MAX_CONCEPT_WORDS = 3


@dataclass(frozen=True)
class ConceptData:
    """The loaded map: concepts, plus the words that are not concepts.

    Stopwords ship *in the same JSON file* rather than as a Python constant
    on purpose. They are the question scaffolding — "how do I", "the command
    I used to" — that carries no shell meaning, and which words those are
    depends on the language the user thinks in. A Spanish speaker who adds
    ``"certificado"`` to their own map needs ``"para"`` dropped just as much
    as an English one needs ``"the"``. Both live in a file they can edit.
    """

    concepts: dict[str, tuple[str, ...]]
    stopwords: frozenset[str]

    @property
    def max_words(self) -> int:
        """Longest concept key in words, for the phrase scan. At least 1."""
        return max((len(k.split()) for k in self.concepts), default=1)


@dataclass(frozen=True)
class QueryGroup:
    """One unit of query meaning: the words typed, plus what they can mean.

    A group is satisfied *literally* when the user's own words appear in the
    command, and *by expansion* when one of the concept's synonyms does. The
    distinction is the whole point — it is what keeps someone who typed
    ``openssl`` from being served everything merely tagged "certificate".
    """

    text: str
    words: tuple[str, ...]
    expansions: tuple[str, ...]

    def literal_hit(self, lowered_command: str) -> bool:
        """True when every word the user typed appears in the command.

        Every word, not the phrase verbatim: a two-word group is exactly the
        conjunction ``search()`` already applies to two separate terms, so
        folding "disk space" into one group can never match less than the
        unfolded query would have.
        """
        return all(word in lowered_command for word in self.words)

    def matched_expansions(self, lowered_command: str) -> tuple[str, ...]:
        """The synonyms of this concept that appear in the command."""
        return tuple(e for e in self.expansions if e in lowered_command)

    def variants(self) -> tuple[str, ...]:
        """Every string that can satisfy this group, for prefiltering.

        The literal words are included: a command matching the query without
        any help from the concept map must still survive the prefilter, or
        expansion would have *removed* a result instead of adding one.
        """
        return self.words + self.expansions


# Loading is cached on (path, mtime, size) rather than done once per process:
# `mem` is a short-lived CLI, but the MCP server and the test suite both live
# long enough to edit ~/.mem/concepts.json underneath a running process, and a
# map that needs a restart to take effect is not the editable file this
# feature is selling.
_cache: dict[tuple[object, ...], ConceptData] = {}


def clear_cache() -> None:
    """Drop the memoised maps. For tests, and for anything that rewrites the map."""
    _cache.clear()


def load(user_path: Path | None = None) -> ConceptData:
    """Load the shipped concept map, layered with the user's if present.

    Layering rules, chosen so that upgrading mem never silently discards a
    user's edits *or* withholds new concepts from them:

    - a concept defined in the user file **replaces** the shipped one of the
      same name, so a synonym list can be corrected rather than only added to;
    - concepts the user does not mention are kept;
    - ``_stopwords`` are **unioned**, because a second language's function
      words are additional to English's, not instead of them.

    A malformed user file is a warning and a fallback to the shipped map, never
    an exception: the search command must not stop working because a hand-edited
    JSON file is missing a comma.
    """
    stat = None
    if user_path is not None:
        try:
            info = user_path.stat()
            stat = (info.st_mtime_ns, info.st_size)
        except OSError:
            stat = None  # No user file (the common case), or unreadable.

    key = (str(user_path) if user_path else None, stat)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    data = _load_shipped()
    if stat is not None and user_path is not None:
        data = _layer_user_map(data, user_path)

    _cache[key] = data
    return data


def _load_shipped() -> ConceptData:
    """Read ``mem/concepts.json`` out of the installed package."""
    try:
        raw = (
            resources.files("mem")
            .joinpath(CONCEPTS_FILENAME)
            .read_text(encoding="utf-8")
        )
        parsed = _parse(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 - a packaging fault, not a user one
        # Unreachable through any supported install; a broken shipped map must
        # still degrade to plain literal search rather than break `mem <query>`.
        _warn(f"built-in concept map could not be read ({exc}); expansion is off")
        return ConceptData(concepts={}, stopwords=frozenset())
    if parsed is None:
        _warn("built-in concept map is malformed; expansion is off")
        return ConceptData(concepts={}, stopwords=frozenset())
    return parsed


def _layer_user_map(shipped: ConceptData, path: Path) -> ConceptData:
    """Merge ``~/.mem/concepts.json`` over the shipped map, or warn and skip."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"could not read {path} ({exc}); using the built-in concept map")
        return shipped

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(
            f"{path} is not valid JSON (line {exc.lineno}: {exc.msg}); "
            "using the built-in concept map instead"
        )
        return shipped

    user = _parse(loaded)
    if user is None:
        _warn(
            f"{path} is not a concept map "
            '(expected {"concept": ["command", ...]}); '
            "using the built-in concept map instead"
        )
        return shipped

    concepts = dict(shipped.concepts)
    concepts.update(user.concepts)
    return ConceptData(
        concepts=concepts,
        stopwords=shipped.stopwords | user.stopwords,
    )


def _parse(loaded: object) -> ConceptData | None:
    """Validate and normalise a decoded JSON document, or return None.

    Returning ``None`` rather than raising keeps the caller's fallback path
    (warn, use the shipped map) the same for every kind of malformation.
    """
    if not isinstance(loaded, dict):
        return None

    concepts: dict[str, tuple[str, ...]] = {}
    stopwords: set[str] = set()

    for key, value in loaded.items():
        if key.startswith(_RESERVED_PREFIX):
            if key == _STOPWORDS_KEY:
                words = _string_list(value)
                if words is None:
                    return None
                # Stopwords are compared against whitespace-split query terms,
                # so unlike a synonym they can never contain a space.
                stopwords.update(word.strip() for word in words)
            continue  # Any other `_key` is a comment.

        entries = _string_list(value)
        if entries is None:
            return None
        concept = " ".join(key.lower().split())
        if not concept or len(concept.split()) > MAX_CONCEPT_WORDS:
            continue
        # A synonym equal to the concept itself is already covered by the
        # literal test, and counting it twice would let a concept vouch for
        # itself as if the map had contributed something.
        seen = {concept}
        kept = []
        for entry in entries:
            if entry and entry not in seen:
                seen.add(entry)
                kept.append(entry)
        if kept:
            concepts[concept] = tuple(kept)

    return ConceptData(concepts=concepts, stopwords=frozenset(stopwords))


def _string_list(value: object) -> list[str] | None:
    """Lowercase a JSON array of strings, or None if it is not one.

    Whitespace is *not* stripped. A trailing space is how the map says "the
    word, not the prefix": ``"ps "`` must not match ``https``, ``"rm "`` must
    not match ``chrome``, and stripping them would have quietly turned two
    dozen precise synonyms into vague ones.
    """
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        if item.strip():
            out.append(item.lower())
    return out


def expand(terms: list[str], data: ConceptData) -> list[QueryGroup]:
    """Group query terms into concepts, each carrying its shell vocabulary.

    Three things happen here, in order:

    1. **Phrases are folded first, longest first.** "disk space" is one
       concept; scanning left to right for the longest key that matches the
       next few words keeps it from decaying into "disk" AND "space", which
       are two much vaguer concepts.
    2. **Stopwords are dropped** — but only those that are not themselves
       concepts, so a map that defines ``"for"`` keeps it.
    3. **Everything else stays as a literal group with no expansions.** A word
       the map has never heard of is still the user's word, and still has to
       be matched; the map's job is to add meaning, never to withhold it.

    If the query is nothing *but* stopwords, they are kept: "how to" is a
    strange thing to search for, but returning results for it beats silently
    searching for nothing.
    """
    groups: list[QueryGroup] = []
    index = 0
    span = min(MAX_CONCEPT_WORDS, data.max_words)
    while index < len(terms):
        for width in range(min(span, len(terms) - index), 0, -1):
            phrase = " ".join(terms[index : index + width])
            expansions = data.concepts.get(phrase)
            if expansions is not None:
                groups.append(
                    QueryGroup(
                        text=phrase,
                        words=tuple(terms[index : index + width]),
                        expansions=expansions,
                    )
                )
                index += width
                break
        else:
            term = terms[index]
            if term not in data.stopwords:
                groups.append(QueryGroup(text=term, words=(term,), expansions=()))
            index += 1

    if not groups:
        return [QueryGroup(text=t, words=(t,), expansions=()) for t in terms]
    return groups


def _warn(message: str) -> None:
    """Report a broken map on stderr, where it cannot corrupt ``--json`` output."""
    print(f"warning: {message}", file=sys.stderr)
