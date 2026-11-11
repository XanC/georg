# SPDX-License-Identifier: AGPL-3.0-only
"""Turn a snapshot plus the mapping file into a reviewable plan.

Terms are identified by WordPress ID; terms the plan creates get a "new:<taxonomy>:<name>"
reference. Each target name resolves to, in order: an existing term with exactly that
name, the most-used term being renamed to it (so its slug survives), or a new term.
"""

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from georg.modules.processors.metadata.references import BOOK_RE, canonical_book
from wp_authority.liturgical import days
from wp_authority.liturgical import series as lectionary_series
from wp_authority.mapping import Canonical, Mapping
from wp_authority.names import (
    has_numeric_ordinal,
    person_name,
    series_key,
    speaker_key,
    spell_ordinals,
)
from wp_authority.scripture import Unparseable, clean_name, overlaps, split_references
from wp_authority.wp import TAXONOMIES

BOOK, SERIES, SPEAKER = TAXONOMIES
Ref = int | str
_TITLE = re.compile(r"^Sermon for (?:the )?(.+?)\s*$", re.I)


@dataclass
class _Tax:
    name: str
    terms: dict[int, dict[str, Any]]
    uses: Counter[int]
    targets: dict[int, list[str]] = field(default_factory=dict)
    resolved: dict[str, Ref] = field(default_factory=dict)
    renames: dict[int, str] = field(default_factory=dict)
    creates: list[str] = field(default_factory=list)
    parents: dict[Ref, Ref] = field(default_factory=dict)
    fixed: set[int] = field(default_factory=set)
    descriptions: dict[Ref, str] = field(default_factory=dict)

    def _rank(self, tid: int, target: str) -> tuple[bool, bool, int, int]:
        """Rename the term that is closest to the target: not hand-fixed, same but for
        dashes and spacing, most used."""
        same = _loose(self.terms[tid]["name"]) == _loose(target)
        return (tid in self.fixed, not same, -self.uses[tid], tid)

    def label(self, ref: Ref) -> str:
        if isinstance(ref, str):
            return ref.split(":", 2)[2]
        if ref not in self.terms:
            return f"#{ref}"
        return self.renames.get(ref) or clean_name(self.terms[ref]["name"])

    def exact(self) -> dict[str, int]:
        best: dict[str, int] = {}
        for tid in sorted(self.terms, key=lambda i: (-self.uses[i], i)):
            best.setdefault(clean_name(self.terms[tid]["name"]), tid)
        return best

    def resolve(self, names: list[str], *, rename: bool = True) -> None:
        exact = self.exact()
        taken = {exact[n] for n in [*names, *self.resolved] if n in exact}
        for name in names:
            if name in self.resolved:
                continue
            if name in exact:
                self.resolved[name] = exact[name]
                continue
            candidates = [
                tid
                for tid, target in self.targets.items()
                if rename
                and target == [name]
                and self.uses[tid]
                and tid not in taken
                and clean_name(self.terms[tid]["name"]) not in names
            ]
            if candidates:
                tid = min(candidates, key=lambda i: self._rank(i, name))
                taken.add(tid)
                self.renames[tid] = name
                self.resolved[name] = tid
            else:
                self.creates.append(name)
                self.resolved[name] = f"new:{self.name}:{name}"


def _loose(name: str) -> str:
    return re.sub(r"[\s,;]+", " ", re.sub(r"\s*[\u2010-\u2015\u2212-]\s*", "-", clean_name(name)))


def _targets(tax: _Tax, tid: int) -> list[Ref]:
    """Where a sermon's term goes; IDs the snapshot doesn't know are left alone."""
    if tid not in tax.terms:
        return [tid]
    return [tax.resolved[n] for n in tax.targets[tid]]


def _book_targets(tax: _Tax, mapping: Mapping, human: list[dict[str, Any]]) -> None:
    rules = mapping.scripture
    fixes = {clean_name(k).casefold(): v for k, v in rules.fix.items()}
    for tid, term in tax.terms.items():
        name = clean_name(term["name"])
        if name.casefold() in fixes:
            refs = [r for raw in fixes[name.casefold()] for r in split_references(raw, rules.dash)]
            tax.targets[tid] = refs
            tax.fixed.add(tid)
        elif canonical_book(name) == name:
            tax.targets[tid] = [name]
        else:
            try:
                tax.targets[tid] = split_references(name, rules.dash)
            except Unparseable as error:
                tax.targets[tid] = [name]
                if tax.uses[tid]:
                    human.append(
                        {
                            "kind": "scripture",
                            "taxonomy": BOOK,
                            "term": tid,
                            "name": name,
                            "sermons": tax.uses[tid],
                            "reason": str(error),
                        }
                    )


def _canonical_targets(tax: _Tax, rules: Canonical) -> None:
    for tid, term in tax.terms.items():
        tax.targets[tid] = [rules.resolve(term["name"])]


def build_plan(snapshot: dict[str, Any], mapping: Mapping) -> dict[str, Any]:
    sermons = {s["id"]: s for s in snapshot["sermons"]}.values()
    taxes: dict[str, _Tax] = {}
    for name in TAXONOMIES:
        terms = {t["id"]: t for t in snapshot["terms"][name]}
        uses: Counter[int] = Counter(
            tid for s in sermons for tid in set(s["terms"].get(name, [])) if tid in terms
        )
        taxes[name] = _Tax(name, terms, uses)
    human: list[dict[str, Any]] = []
    _book_targets(taxes[BOOK], mapping, human)
    _canonical_targets(taxes[SERIES], mapping.series)
    _canonical_targets(taxes[SPEAKER], mapping.speakers)

    assigned_series = {int(k): mapping.series.resolve(v) for k, v in mapping.series.assign.items()}
    for tax in taxes.values():
        needed = [n for tid, ns in tax.targets.items() if tax.uses[tid] for n in ns]
        if tax.name == SERIES:
            needed += list(assigned_series.values())
        tax.resolve(list(dict.fromkeys(needed)))

    after: dict[tuple[int, str], list[Ref]] = {}
    for s in sermons:
        for name, tax in taxes.items():
            before = list(s["terms"].get(name, []))
            new = [r for t in before for r in _targets(tax, t)]
            if name == SERIES and s["id"] in assigned_series:
                new = [tax.resolved[assigned_series[s["id"]]]]
            after[(s["id"], name)] = list(dict.fromkeys(new))

    _nesting(taxes, mapping, after, human)
    plan: dict[str, Any] = {
        "format": 1,
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mapping": mapping.model_dump(),
    }
    plan["sermon_changes"], assigns = _changes(sermons, taxes, after)
    used = {(name, ref) for (_, name), refs in after.items() for ref in refs}
    plan["series_fill"] = _series_fill(sermons, taxes, after, mapping, human, assigned_series)
    exact = taxes[SERIES].exact()
    proposed = {(SERIES, exact[f["series"]]) for f in plan["series_fill"] if f["series"] in exact}
    deletes = _deletes(taxes, used | proposed) if mapping.unused.delete else []
    plan["terms"] = _term_report(taxes, used, deletes)
    plan["suggestions"] = _suggestions(taxes, used, mapping)
    plan["needs_human"] = human
    plan["actions"] = _actions(taxes, assigns, deletes)
    return plan


def _nesting(
    taxes: dict[str, _Tax],
    mapping: Mapping,
    after: dict[tuple[int, str], list[Ref]],
    human: list[dict[str, Any]],
) -> None:
    books = taxes[BOOK]
    if mapping.scripture.nest_under_books:
        refs = {ref for (_, name), rs in after.items() if name == BOOK for ref in rs}
        wanted: dict[Ref, str] = {}
        for ref in refs:
            label = books.label(ref)
            match = BOOK_RE.match(label)
            book = canonical_book(match.group(0)) if match else None
            if book and book != label:
                wanted[ref] = book
        books.resolve(sorted(set(wanted.values())), rename=False)
        for ref, book in wanted.items():
            books.parents[ref] = books.resolved[book]
    speakers = taxes[SPEAKER]
    for person, entry in mapping.speakers.persons().items():
        speakers.resolve([clean_name(person)], rename=False)
        parent = speakers.resolved[clean_name(person)]
        current = speakers.terms[parent]["description"] if isinstance(parent, int) else ""
        if entry.description is not None and entry.description != current:
            speakers.descriptions[parent] = entry.description
        for member in entry.terms:
            target = mapping.speakers.resolve(member)
            member_ref = speakers.resolved.get(target) or speakers.exact().get(target)
            if member_ref is None:
                human.append(
                    {
                        "kind": "speaker",
                        "taxonomy": SPEAKER,
                        "name": member,
                        "reason": f"listed under [speakers.people] {person!r} but no such term",
                    }
                )
            elif member_ref != parent:
                speakers.parents[member_ref] = parent


def _changes(
    sermons: Any, taxes: dict[str, _Tax], after: dict[tuple[int, str], list[Ref]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, actions = [], []
    for s in sermons:
        for name, tax in taxes.items():
            before = sorted(s["terms"].get(name, []))
            new = after[(s["id"], name)]
            labels_before = [tax.label(t) for t in before]
            labels_after = [tax.label(t) for t in new]
            if sorted(new, key=str) == sorted(before, key=str):
                continue
            rows.append(
                {
                    "post": s["id"],
                    "date": s["date"][:10],
                    "title": clean_name(s["title"]),
                    "taxonomy": name,
                    "before": labels_before,
                    "after": labels_after,
                }
            )
            actions.append(
                {"op": "assign", "taxonomy": name, "post": s["id"], "before": before, "after": new}
            )
    return rows, actions


def _deletes(taxes: dict[str, _Tax], used: set[tuple[str, Ref]]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for name, tax in taxes.items():
        keep: set[int] = {r for r in tax.resolved.values() if isinstance(r, int)}
        keep |= {r for (n, r) in used if n == name and isinstance(r, int)}
        keep |= {p for p in tax.parents.values() if isinstance(p, int)}
        gone = set(tax.terms) - keep
        while True:
            parents = {
                tax.parents.get(tid, tax.terms[tid]["parent"])
                for tid in tax.terms
                if tid not in gone
            }
            blocked = gone & parents
            if not blocked:
                break
            gone -= blocked

        out += [(name, tid) for tid in sorted(gone, key=lambda t: (-_depth(tax, t), t))]
    return out


def _depth(tax: _Tax, tid: int) -> int:
    level, seen = 0, set()
    while tid and tid in tax.terms and tid not in seen:
        seen.add(tid)
        tid = tax.terms[tid]["parent"]
        level += 1
    return level


def _term_report(
    taxes: dict[str, _Tax], used: set[tuple[str, Ref]], deletes: list[tuple[str, int]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    deleted = set(deletes)
    for name, tax in taxes.items():
        for label in tax.creates:
            rows.append(
                {"action": "create", "taxonomy": name, "term": None, "name": label, "into": []}
            )
        for tid, label in tax.renames.items():
            rows.append(
                {
                    "action": "rename",
                    "taxonomy": name,
                    "term": tid,
                    "name": clean_name(tax.terms[tid]["name"]),
                    "into": [label],
                }
            )
        for tid in sorted(tax.terms):
            targets = [tax.resolved[n] for n in tax.targets[tid] if n in tax.resolved]
            if not tax.uses[tid] or targets == [tid] or tid in tax.renames:
                continue
            action = "split" if len(targets) > 1 else "merge" if targets else "drop"
            rows.append(
                {
                    "action": action,
                    "taxonomy": name,
                    "term": tid,
                    "name": clean_name(tax.terms[tid]["name"]),
                    "into": [tax.label(t) for t in targets],
                }
            )
        for child, parent in tax.parents.items():
            current = tax.terms[child]["parent"] if isinstance(child, int) else 0
            if current != parent:
                rows.append(
                    {
                        "action": "parent",
                        "taxonomy": name,
                        "term": child if isinstance(child, int) else None,
                        "name": tax.label(child),
                        "into": [tax.label(parent)],
                    }
                )
        for ref, text in tax.descriptions.items():
            rows.append(
                {
                    "action": "describe",
                    "taxonomy": name,
                    "term": ref if isinstance(ref, int) else None,
                    "name": tax.label(ref),
                    "into": [text],
                }
            )
        for tid in sorted(tax.terms):
            if (name, tid) in deleted:
                was = "unused" if not tax.uses[tid] else "emptied"
                rows.append(
                    {
                        "action": f"delete-{was}",
                        "taxonomy": name,
                        "term": tid,
                        "name": clean_name(tax.terms[tid]["name"]),
                        "into": [],
                    }
                )
    return rows


def _final_names(tax: _Tax, used: set[tuple[str, Ref]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for tid, n in tax.uses.items():
        for name in tax.targets[tid]:
            if (tax.name, tax.resolved.get(name)) in used:
                counts[name] += n
    return counts


def _suggestions(
    taxes: dict[str, _Tax], used: set[tuple[str, Ref]], mapping: Mapping
) -> dict[str, dict[str, list[str]]]:
    series = _final_names(taxes[SERIES], used)
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in series:
        grouped[series_key(name)].append(name)
    out_series: dict[str, list[str]] = {}
    for names in grouped.values():
        best = min(names, key=lambda n: (has_numeric_ordinal(n), -series[n], len(n), n))
        canonical = spell_ordinals(best)
        aliases = sorted(n for n in names if n != canonical)
        if aliases:
            out_series[canonical] = aliases
    speakers = _final_names(taxes[SPEAKER], used)
    by_key: dict[str, list[str]] = defaultdict(list)
    by_person: dict[str, list[str]] = defaultdict(list)
    for name in speakers:
        by_key[speaker_key(name)].append(name)
        person = person_name(name)
        if person:
            by_person[speaker_key(person)].append(name)
    out_speakers: dict[str, list[str]] = {}
    for names in by_key.values():
        if len(names) > 1:
            best = min(names, key=lambda n: (-speakers[n], n))
            out_speakers[best] = sorted(n for n in names if n != best)
    listed = {
        mapping.speakers.resolve(m) for p in mapping.speakers.persons().values() for m in p.terms
    }
    out_people: dict[str, list[str]] = {}
    for names in by_person.values():
        distinct = sorted(set(names) - listed)
        if len(set(names)) > 1 and distinct:
            person = person_name(names[0])
            assert person is not None
            out_people[person] = sorted(set(names))
    return {"series": out_series, "speakers": out_speakers, "people": out_people}


def _series_fill(
    sermons: Any,
    taxes: dict[str, _Tax],
    after: dict[tuple[int, str], list[Ref]],
    mapping: Mapping,
    human: list[dict[str, Any]],
    assigned: dict[int, str],
) -> list[dict[str, Any]]:
    series = taxes[SERIES]
    labels: Counter[str] = Counter(
        series.label(r) for (_, n), refs in after.items() if n == SERIES for r in refs
    )
    for tid in series.terms:
        labels.update({name: 0 for name in series.targets[tid]})
    existing: dict[str, str] = {}
    for label in sorted(labels, key=lambda n: (has_numeric_ordinal(n), -labels[n], n)):
        existing.setdefault(series_key(label), label)
    proposals = []
    for s in sermons:
        if after[(s["id"], SERIES)] or s["id"] in assigned or s["status"] == "trash":
            continue
        day = date.fromisoformat(s["date"][:10])
        lectionary = f"three-year {lectionary_series(day)}"
        refs = [taxes[BOOK].label(r) for r in after[(s["id"], BOOK)]]
        title = _TITLE.match(clean_name(s["title"]))
        title_key = series_key(mapping.series.resolve(title[1])) if title else None
        found = []
        for candidate in days(day):
            names = [mapping.series.resolve(n) for n in candidate.names]
            term = next((existing[series_key(n)] for n in names if series_key(n) in existing), None)
            keys = {series_key(n) for n in names} | ({series_key(term)} if term else set())
            clues = ["date"]
            if refs and overlaps(refs, candidate.readings()):
                clues.append("scripture")
            if title_key and title_key in keys:
                clues.append("title")
            found.append((candidate, term or candidate.name, clues))
        agree = [f for f in found if len(f[2]) > 1]
        base = {"post": s["id"], "date": s["date"][:10], "title": clean_name(s["title"])}
        if len(agree) == 1:
            candidate, term, clues = agree[0]
            proposals.append(
                {
                    **base,
                    "series": term,
                    "day": candidate.name,
                    "lectionary": lectionary,
                    "clues": clues,
                    "new_term": series_key(term) not in existing,
                }
            )
        else:
            reason = "clues disagree" if not agree else "several days agree"
            if not found:
                reason = "no liturgical day on this date"
            human.append(
                {
                    "kind": "series",
                    "taxonomy": SERIES,
                    **base,
                    "lectionary": lectionary,
                    "scripture": refs,
                    "title_day": title[1] if title else None,
                    "candidates": [f[1] for f in found],
                    "reason": reason,
                }
            )
    return proposals


def _actions(
    taxes: dict[str, _Tax], assigns: list[dict[str, Any]], deletes: list[tuple[str, int]]
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for name, tax in taxes.items():
        parents = set(tax.parents.values())
        for label in sorted(tax.creates, key=lambda n: tax.resolved[n] not in parents):
            actions.append(
                {"op": "create", "taxonomy": name, "ref": tax.resolved[label], "name": label}
            )
    for name, tax in taxes.items():
        for tid, label in tax.renames.items():
            actions.append({"op": "rename", "taxonomy": name, "term": tid, "name": label})
    for name, tax in taxes.items():
        for child, parent in tax.parents.items():
            if not isinstance(child, int) or tax.terms[child]["parent"] != parent:
                actions.append({"op": "parent", "taxonomy": name, "term": child, "parent": parent})
    for name, tax in taxes.items():
        for ref, text in tax.descriptions.items():
            actions.append({"op": "describe", "taxonomy": name, "term": ref, "description": text})
    actions += assigns
    actions += [{"op": "delete", "taxonomy": n, "term": tid} for n, tid in deletes]
    return actions


def write_csv(plan: dict[str, Any], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["SERMON CHANGES"])
        out.writerow(["date", "title", "taxonomy", "before", "after", "post"])
        for row in sorted(plan["sermon_changes"], key=lambda r: (r["date"], r["post"])):
            out.writerow(
                [
                    row["date"],
                    row["title"],
                    row["taxonomy"],
                    "; ".join(row["before"]),
                    "; ".join(row["after"]),
                    row["post"],
                ]
            )
        out.writerow([])
        out.writerow(["TERMS"])
        out.writerow(["action", "taxonomy", "term", "name", "into"])
        for row in plan["terms"]:
            out.writerow(
                [
                    row["action"],
                    row["taxonomy"],
                    row["term"] or "",
                    row["name"],
                    "; ".join(row["into"]),
                ]
            )
        out.writerow([])
        out.writerow(["NEEDS A HUMAN"])
        out.writerow(
            ["kind", "taxonomy", "term or post", "name or title", "date", "candidates", "reason"]
        )
        for row in plan["needs_human"]:
            out.writerow(
                [
                    row["kind"],
                    row["taxonomy"],
                    row.get("term") or row.get("post", ""),
                    row.get("name") or row.get("title", ""),
                    row.get("date", ""),
                    "; ".join(row.get("candidates", [])),
                    row["reason"],
                ]
            )
        out.writerow([])
        out.writerow(["SUGGESTED MERGES (not applied; copy into the mapping file to apply)"])
        out.writerow(["table", "canonical", "aliases"])
        for table, entries in plan["suggestions"].items():
            section = {
                "series": "series.canonical",
                "speakers": "speakers.canonical",
                "people": "speakers.people",
            }[table]
            for canonical, aliases in entries.items():
                out.writerow([section, canonical, "; ".join(aliases)])
        out.writerow([])
        out.writerow(["PROPOSED SERIES (not applied; copy into [series.assign] to apply)"])
        out.writerow(["date", "title", "post", "series", "clues", "new term"])
        for row in plan["series_fill"]:
            out.writerow(
                [
                    row["date"],
                    row["title"],
                    row["post"],
                    row["series"],
                    "+".join(row["clues"]),
                    "yes" if row["new_term"] else "",
                ]
            )
