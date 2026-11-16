#!/usr/bin/env python3
"""Faux agent déterministe pour les tests : il joue les cinq rôles du pipeline sans LLM.

Il lit l'invite dans $MEMEX_PROMPT_FILE, reconnaît le rôle à sa première phrase
et agit dans le répertoire courant. FAKE_AGENT_MODE permet de simuler les
comportements fautifs que les garde-fous doivent arrêter.

Dans les transcripts de test, une ligne `DECISION: titre | contenu | raison`
(ou FAIT:, PISTE:) déclare un élément à extraire.
"""
import json
import os
import re
import sys
from pathlib import Path

MODE = os.environ.get("FAKE_AGENT_MODE", "ok")
prompt = Path(os.environ["MEMEX_PROMPT_FILE"]).read_text(encoding="utf-8")
calls = os.environ.get("FAKE_AGENT_CALLS")
if calls:
    with open(calls, "a", encoding="utf-8") as handle:
        handle.write(prompt.split("\n", 2)[1][:60] + "\n")

if MODE == "fail":
    sys.exit(1)


def block(after: str) -> str:
    match = re.search(re.escape(after) + r".*?```json\n(.*?)\n```", prompt, re.S)
    return match.group(1) if match else "{}"


def page(kind: str, title: str, summary: str, body: str, extra: str = "") -> str:
    return f"---\ntype: {kind}\ntitre: {title}\nresume: {summary}\n{extra}---\n\n{body}\n"


def extract() -> None:
    if MODE == "garbage":
        print("Désolé, je ne peux pas répondre.")
        return
    transcript = prompt.split("<condense>", 1)[1].rsplit("</condense>", 1)[0]
    kinds = {"DECISION": "decision", "FAIT": "fait", "PISTE": "piste_abandonnee"}
    elements = []
    for line in transcript.splitlines():
        match = re.match(r"^(DECISION|FAIT|PISTE): (.*)$", line.strip())
        if match:
            parts = [p.strip() for p in match.group(2).split("|")] + ["", ""]
            elements.append({
                "type": kinds[match.group(1)], "titre": parts[0], "contenu": parts[1] or parts[0],
                "raison": parts[2], "entites": re.findall(r"@(\w+)", line), "explicite": False,
            })
    subject = re.search(r"SUJET: (.*)", transcript)
    print("Voici le résultat :\n```json\n" + json.dumps({
        "resume": "Session de test.", "sujet": subject.group(1).strip() if subject else "", "elements": elements,
    }, ensure_ascii=False) + "\n```")


def integrate() -> None:
    elements = json.loads(block("## Éléments à intégrer"))
    existing = sorted(p for p in Path(".").rglob("*.md") if p.parts[0] in ("Projets", "Sujets"))
    where = re.search(r"Dossier : `([^`]+)`", prompt)
    if where:
        dossier = where.group(1)
    else:
        theme = re.search(r"Thème repéré : « ([^»]+) »", prompt)
        dossier = "Sujets/" + (theme.group(1).strip() if theme else "Divers")
    name = dossier.split("/")[-1]
    date = re.search(r"- Date : (\d{4}-\d{2}-\d{2})", prompt).group(1)
    main = Path(dossier) / f"{name}.md"
    main.parent.mkdir(parents=True, exist_ok=True)
    if not main.exists():
        main.write_text(page(dossier.split("/")[0][:-1].lower(), name, f"Page de {name}.", f"# {name}\n\n## En bref\n\nDossier de test."), encoding="utf-8")
    text = main.read_text(encoding="utf-8")
    for element in elements:
        if element["type"] == "decision":
            fiche = Path(dossier) / "Décisions" / f"{date} {element['titre']}.md"
            fiche.parent.mkdir(parents=True, exist_ok=True)
            entities = "".join(f"  - {e}\n" for e in element["entites"])
            fiche.write_text(page(
                "decision", element["titre"], element["contenu"],
                f"## Décision\n\n{element['contenu']}\n\n## Pourquoi\n\n{element['raison']}",
                f"date: {date}\nstatut: active\n" + (f"entites:\n{entities}" if entities else ""),
            ), encoding="utf-8")
            text += f"\n## Décisions\n\n- [[{dossier}/Décisions/{date} {element['titre']}|{element['titre']}]] — {element['contenu']}\n"
        else:
            text += f"\n- {element['titre']} : {element['contenu']}\n"
    if any(e["entites"] for e in elements) and "entites:" not in text:
        entities = sorted({e for el in elements for e in el["entites"]})
        text = text.replace("---\n\n", "entites:\n" + "".join(f"  - {e}\n" for e in entities) + "---\n\n", 1)
    main.write_text(text, encoding="utf-8")
    if MODE == "delete_once" and "refusée" not in prompt or MODE == "delete":
        victims = [p for p in existing if p != main and p.exists()]
        (victims[0] if victims else main).unlink()
    if MODE == "edit_decision":
        for fiche in Path(".").rglob("Décisions/*.md"):
            if date not in fiche.name:
                fiche.write_text(fiche.read_text(encoding="utf-8") + "\nRéécriture interdite.\n", encoding="utf-8")
    if MODE == "outside":
        Path("index.md").write_text("écrasé", encoding="utf-8")
        Path("Notes en vrac.md").write_text("hors dossier", encoding="utf-8")
        Path(".memex/ledger.jsonl").write_text("falsifié\n", encoding="utf-8")
    Path(".memex-result.json").write_text(json.dumps({"dossier": dossier, "resume": "Intégration de test."}), encoding="utf-8")


def tisseur() -> None:
    report = json.loads(block("## Ce qu'un contrôle automatique a repéré"))
    for item in report.get("mentions_sans_lien", []):
        target = Path(item["page"])
        text = target.read_text(encoding="utf-8")
        head, body = text.split("---\n\n", 1) if text.startswith("---") else ("", text)
        link = f"[[{item['cible'][:-3]}|{item['terme']}]]"
        body = re.sub(r"(?<![\w\[|])" + re.escape(item["terme"]) + r"(?![\w\]|])", lambda _m: link, body, count=1)
        if MODE == "rewrite":
            body += "\nPhrase ajoutée par le tisseur.\n"
        target.write_text((head + "---\n\n" if head else "") + body, encoding="utf-8")
    for item in report.get("concepts_candidats", []):
        links = "\n".join(f"- [[{p[:-3]}]]" for p in item["pages"])
        Path("Concepts").mkdir(exist_ok=True)
        Path("Concepts", f"{item['entite']}.md").write_text(
            page("concept", item["entite"], f"Notion commune à {len(item['dossiers'])} dossiers.", f"# {item['entite']}\n\n{links}"),
            encoding="utf-8",
        )


def diagnose() -> None:
    actions = []
    for candidate in sorted(Path(".").rglob("*Doublon*.md")):
        actions.append({
            "type": "suppression", "titre": f"Supprimer {candidate.name}", "pages": [candidate.as_posix()],
            "raison": "Page entièrement reprise ailleurs.", "consigne": f"Supprime {candidate.as_posix()}.",
        })
    for candidate in sorted(Path(".").rglob("*Long*.md")):
        actions.append({
            "type": "condensation", "titre": f"Condenser {candidate.name}", "pages": [candidate.as_posix()],
            "raison": "Page répétitive.", "consigne": "Retire les répétitions.",
        })
    print(json.dumps({"actions": actions, "questions": [{"titre": "Déploiement manuel ou scripté ?", "detail": "Deux pages se contredisent."}]}, ensure_ascii=False))


def apply() -> None:
    action = json.loads(block("## L'action à appliquer"))
    for name in action["pages"]:
        target = Path(name)
        if action["type"] == "suppression":
            target.unlink()
        else:
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace("\nRépétition.", "") + "\nCondensé.\n", encoding="utf-8")
    Path(".memex-result.json").write_text(json.dumps({"fait": True, "resume": "Action de test appliquée."}), encoding="utf-8")


if "Tu es l'extracteur" in prompt:
    extract()
elif "Tu es l'archiviste" in prompt:
    integrate()
elif "Tu es le tisseur" in prompt:
    tisseur()
elif "Tu dois proposer une courte liste" in prompt:
    diagnose()
elif "Tu dois appliquer une seule action" in prompt:
    apply()
else:
    sys.exit(3)
