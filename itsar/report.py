"""Phase 5: compile a structured ITSAR compliance report.

One structured document model renders two artefacts:
  * Markdown  -- the reviewable source of record
  * standalone HTML -- self-contained (images embedded), for circulation

Both are generated from the database, so a report can be regenerated at any
time and will match the stored evidence hashes.
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from typing import Any

from .repo import Repo, utcnow

# --------------------------------------------------------------------- model
class Doc:
    def __init__(self, title: str):
        self.title = title
        self.blocks: list[tuple[str, Any]] = []

    def h(self, level: int, text: str) -> "Doc":
        self.blocks.append(("h", (level, text)))
        return self

    def p(self, text: str) -> "Doc":
        self.blocks.append(("p", text))
        return self

    def table(self, headers: list[str], rows: list[list[Any]]) -> "Doc":
        self.blocks.append(("table", (headers, rows)))
        return self

    def ul(self, items: list[str]) -> "Doc":
        self.blocks.append(("ul", items))
        return self

    def images(self, items: list[tuple[str, str]]) -> "Doc":
        self.blocks.append(("images", items))
        return self

    def rule(self) -> "Doc":
        self.blocks.append(("rule", None))
        return self

    # ------------------------------------------------------------- markdown
    def to_markdown(self) -> str:
        out: list[str] = []
        for kind, payload in self.blocks:
            if kind == "h":
                lvl, text = payload
                out += ["#" * lvl + " " + text, ""]
            elif kind == "p":
                out += [payload, ""]
            elif kind == "ul":
                out += [f"- {i}" for i in payload] + [""]
            elif kind == "table":
                headers, rows = payload
                out.append("| " + " | ".join(_md(c) for c in headers) + " |")
                out.append("|" + "|".join("---" for _ in headers) + "|")
                for r in rows:
                    out.append("| " + " | ".join(_md(c) for c in r) + " |")
                out.append("")
            elif kind == "images":
                for path, caption in payload:
                    out += [f"![{caption}](evidence/{Path(path).name})", f"*{caption}*", ""]
            elif kind == "rule":
                out += ["---", ""]
        return "\n".join(out)

    # ----------------------------------------------------------------- html
    def to_html(self, title_suffix: str = "") -> str:
        body: list[str] = []
        for kind, payload in self.blocks:
            if kind == "h":
                lvl, text = payload
                body.append(f"<h{lvl}>{html.escape(text)}</h{lvl}>")
            elif kind == "p":
                body.append(f"<p>{_inline(payload)}</p>")
            elif kind == "ul":
                body.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in payload) + "</ul>")
            elif kind == "table":
                headers, rows = payload
                head = "".join(f"<th>{_inline(h)}</th>" for h in headers)
                trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                              for r in rows)
                body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table>")
            elif kind == "images":
                for path, caption in payload:
                    data_uri = _data_uri(path)
                    body.append(
                        f'<figure><img src="{data_uri}" alt="{html.escape(caption)}"/>'
                        f"<figcaption>{html.escape(caption)}</figcaption></figure>")
            elif kind == "rule":
                body.append("<hr/>")
        return _HTML_SHELL.format(title=html.escape(self.title + title_suffix),
                                  body="\n".join(body),
                                  generated=utcnow())


_HTML_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
 :root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --accent:#0b6bcb;
          --pass:#15803d; --fail:#b91c1c; --warn:#b45309; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; padding:48px 24px 96px; background:#f8fafc; color:var(--ink);
         font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif; }}
 main {{ max-width:1040px; margin:0 auto; background:#fff; padding:56px 64px;
         border:1px solid var(--line); border-radius:14px;
         box-shadow:0 1px 2px rgba(15,23,42,.06),0 12px 32px -12px rgba(15,23,42,.12); }}
 h1 {{ font-size:30px; line-height:1.25; margin:0 0 20px; letter-spacing:-.02em; }}
 h2 {{ font-size:21px; margin:40px 0 12px; padding-bottom:8px; border-bottom:2px solid var(--line);
       letter-spacing:-.01em; }}
 h3 {{ font-size:17px; margin:28px 0 8px; color:#1e293b; }}
 h4 {{ font-size:15px; margin:20px 0 6px; color:var(--muted); text-transform:uppercase;
       letter-spacing:.06em; }}
 p, li {{ color:#1e293b; }}
 ul {{ padding-left:22px; }}
 table {{ width:100%; border-collapse:collapse; margin:14px 0 22px; font-size:13.5px; }}
 th {{ text-align:left; background:#f1f5f9; font-weight:600; padding:9px 11px;
       border-bottom:1px solid var(--line); vertical-align:bottom; }}
 td {{ padding:9px 11px; border-bottom:1px solid var(--line); vertical-align:top; }}
 tr:last-child td {{ border-bottom:none; }}
 code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; font-size:.9em;
         font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
 figure {{ margin:18px 0; }}
 img {{ max-width:100%; border:1px solid var(--line); border-radius:8px; display:block; }}
 figcaption {{ font-size:12.5px; color:var(--muted); margin-top:6px; }}
 hr {{ border:none; border-top:1px solid var(--line); margin:34px 0; }}
 .tag {{ display:inline-block; padding:1px 7px; border-radius:999px; font-size:12px;
         font-weight:600; letter-spacing:.02em; }}
 .pass {{ color:var(--pass); }} .fail {{ color:var(--fail); }} .ns {{ color:var(--warn); }}
 footer {{ max-width:1040px; margin:18px auto 0; color:var(--muted); font-size:12px; }}
 @media print {{ body {{ background:#fff; padding:0; }} main {{ border:none; box-shadow:none; padding:0; }} }}
</style></head>
<body><main>
{body}
</main>
<footer>Generated {generated} by the ITSAR Compliance Agent. Evidence hashes in this report are
SHA-256 of the stored artefacts; any edit invalidates them.</footer>
</body></html>"""


def _md(v: Any) -> str:
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", "<br>")


def _inline(v: Any) -> str:
    s = html.escape(str(v if v is not None else ""))
    out, in_code = [], False
    for part in s.split("`"):
        out.append(f"<code>{part}</code>" if in_code else part)
        in_code = not in_code
    s = "".join(out)
    while "**" in s:
        s = s.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    return s


def _data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    mime = "image/png" if p.suffix.lower() == ".png" else "image/svg+xml"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


# ------------------------------------------------------------------ builder
def build(repo: Repo, device: dict, meta: dict, family_name: str, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ev_dir = out_dir / "evidence"
    ev_dir.mkdir(exist_ok=True)

    verdicts = repo.verdicts_for_device(device["id"])
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["result"]] = counts.get(v["result"], 0) + 1

    doc = Doc(f"ITSAR Compliance Assessment Report — {device['model']}")

    doc.h(1, f"ITSAR Compliance Assessment Report")
    doc.table(["Field", "Value"], [
        ["Device under test", f"**{device['model']}**"],
        ["Firmware under test", device["firmware"]],
        ["Device family", family_name],
        ["Management address", f"{device['mgmt_protocol']}://{device['host']}"],
        ["Credential reference", f"`{device['credential_ref']}` (secret not stored)"],
        ["Assessment standard", meta.get("standard", "ITSAR")],
        ["Standard version", meta.get("standard_version", "n/a")],
        ["Assessment basis", "Placeholder input; exploratory automated run (not an approved assessment)"],
        ["Report generated", utcnow()],
        ["Report ID", f"ITSAR-{device['model']}-{device['firmware']}".replace(" ", "")],
    ])

    doc.h(2, "1. Executive summary")
    total = len(verdicts)
    passed = counts.get("pass", 0)
    failed = counts.get("fail", 0)
    ns = counts.get("not_supported", 0)
    pct = (100.0 * passed / total) if total else 0.0
    doc.p(f"{total} scenario(s) were executed against **{device['model']}** running firmware "
          f"**{device['firmware']}**. **{passed}** passed, **{failed}** failed, **{ns}** were "
          f"recorded as not supported by this firmware. Automated compliance for the tested "
          f"scope is **{pct:.1f}%**. This is a heuristic prototype result, not a compliance determination.")
    if ns:
        doc.p(f"{ns} scenario(s) could not be evaluated because the required capability is not "
              f"present in this firmware build. These are reported as *Not Supported* and are not "
              f"counted as failures; they require a design-level disposition.")
    doc.table(["Result", "Scenarios", "Share"],
              [[k, v, f"{100.0 * v / total:.0f}%" if total else "0%"]
               for k, v in sorted(counts.items())])

    doc.p("PROTOTYPE ONLY: synthetic inputs and mock results must not be used as evidence of ITSAR compliance. Criteria matching is heuristic and requires expert validation.")
    doc.h(2, "2. Scope and methodology")
    doc.ul([
        "**Phase 1 — preconditions.** The prototype attempted catalogue-supported probes and remediation.",
        "**Phase 2 — dry run.** The prototype attempts read-only discovery; inspect the job log for results.",
        "**Phase 3 — execution.** Scenario jobs and attempted state locks are recorded; their sufficiency is not independently validated.",
        "**Phase 4 — reuse.** Family reuse is experimental; consult the job log for what actually ran.",
        "**Evidence policy.** The prototype rejects certain recognisable error text and failed locks; this is not a guarantee of screenshot correctness.",
    ])

    doc.h(2, "3. Results by clause")
    rows = []
    for v in verdicts:
        badge = {"pass": "PASS", "fail": "FAIL", "not_supported": "NOT SUPPORTED",
                 "partial": "PARTIAL", "blocked": "BLOCKED"}.get(v["result"], v["result"].upper())
        ev_count = len(json.loads(v["evidence_ids_json"] or "[]"))
        rows.append([v["clause_no"], v["scenario_no"], f"**{badge}**",
                     f"{ev_count} artefact(s)", v["notes"] or ""])
    doc.table(["Clause", "Scenario", "Verdict", "Evidence", "Basis"], rows)

    doc.h(2, "4. Detailed findings")
    for v in verdicts:
        job = repo.get_job(v["job_id"])
        scenario = repo.db.query_one("SELECT * FROM scenario WHERE id=?", (job["scenario_id"],))
        clause = repo.db.query_one("SELECT * FROM clause WHERE id=?", (job["clause_id"],))
        doc.h(3, f"Clause {v['clause_no']} / {v['scenario_no']} — "
                 f"{scenario['title'] if scenario else ''}")
        doc.p(f"**Verdict:** {v['result'].replace('_', ' ').upper()}  ·  "
              f"**Requirement:** {clause['requirement_text'] if clause else ''}")
        criteria = json.loads(v["criteria_met_json"] or "[]")
        if criteria:
            doc.h(4, "Pass criteria")
            doc.table(["Criterion", "Met", "Matched evidence terms"],
                      [[c.get("criterion", ""), "yes" if c.get("met") else "**NO**",
                        ", ".join(c.get("matched_terms", []))[:90]] for c in criteria])
        trace = repo.db.query_one(
            "SELECT content_json FROM plan WHERE device_id=? AND phase=3 AND scenario_id=? "
            "ORDER BY version DESC LIMIT 1", (device["id"], job["scenario_id"]))
        if trace:
            steps = json.loads(trace["content_json"]).get("steps", [])
            doc.h(4, "Executed steps")
            doc.table(["#", "Step", "Tool / op", "Outcome", "Heals"],
                      [[s["step_no"], (s.get("intent") or "")[:80],
                        f"`{s['tool']}/{s['op']}`",
                        "pass" if s["ok"] else ("not supported" if s.get("not_supported") else "fail"),
                        s.get("healed", 0)] for s in steps])
        ev_ids = json.loads(v["evidence_ids_json"] or "[]")
        if ev_ids:
            doc.h(4, "Evidence")
            imgs: list[tuple[str, str]] = []
            for ev_id in ev_ids:
                ev = repo.db.query_one("SELECT * FROM evidence WHERE id=?", (ev_id,))
                if not ev or not Path(ev["file_path"]).exists():
                    continue
                target = ev_dir / Path(ev["file_path"]).name
                target.write_bytes(Path(ev["file_path"]).read_bytes())
                cap = (f"Job {ev['job_id']} · captured {ev['captured_at']} · "
                       f"lock {ev['lock_kind']} ({ev['lock_detail']}) · sha256 {ev['sha256'][:16]}…")
                imgs.append((str(target), cap))
            if imgs:
                doc.images(imgs)
            else:
                doc.p("_Evidence artefacts are referenced in the database but were not found on "
                      "disk; the report cannot embed them._")

    heals = []
    for v in verdicts:
        for h in repo.heals_for_job(v["job_id"]):
            heals.append([v["scenario_no"], h["attempt"], h["strategy"], h["diagnosis"],
                          f"v{h['plan_version_before']}→v{h['plan_version_after']}"
                          if h["plan_version_after"] else "not applied"])
    doc.h(2, "5. Self-healing and deviations")
    if heals:
        doc.p("The following deviations from the plan were detected and corrected during execution. "
              "Each produced a new immutable plan version; the pre-execution plan was not overwritten.")
        doc.table(["Scenario", "Attempt", "Strategy", "Diagnosis", "Plan version"], heals)
    else:
        doc.p("No plan deviations occurred during this assessment.")
    unsupported = [v for v in verdicts if v["result"] == "not_supported"]
    if unsupported:
        doc.h(3, "Not-supported capabilities")
        doc.table(["Clause", "Scenario", "Detail"],
                  [[v["clause_no"], v["scenario_no"], v["notes"] or ""] for v in unsupported])

    doc.h(2, "6. Automation assets produced")
    dev = repo.get_device(device["id"])
    siblings = repo.device_family_members(dev["family_id"])
    sib_ids = [s["id"] for s in siblings]
    scripts = repo.db.query(
        f"SELECT s.*, d.model AS dev_model, d.firmware AS dev_fw FROM script s "
        f"JOIN device d ON d.id=s.device_id WHERE s.device_id IN "
        f"({','.join('?' for _ in sib_ids)}) ORDER BY s.scenario_id, s.device_id, s.version",
        tuple(sib_ids))
    if scripts:
        doc.table(["Scenario", "Device", "Firmware", "Version", "Origin", "File"],
                  [[repo.db.scalar("SELECT scenario_no FROM scenario WHERE id=?", (s["scenario_id"],), ""),
                    s["dev_model"], s["dev_fw"], f"**v{s['version']}**",
                    "healed" if s["parent_script_id"] else "authored",
                    f"`{Path(s['file_path']).name if s['file_path'] else '(db only)'}`"]
                   for s in scripts])
    else:
        doc.p("_No scripts were produced._")

    rejected = []
    for v in verdicts:
        for r in repo.rejections_for_job(v["job_id"]):
            rejected.append([v["scenario_no"], r["reason"], (r["detail"] or "")[:120]])
    doc.h(2, "7. Evidence integrity annex")
    ev_rows = []
    for v in verdicts:
        for ev_id in json.loads(v["evidence_ids_json"] or "[]"):
            ev = repo.db.query_one("SELECT * FROM evidence WHERE id=?", (ev_id,))
            if ev:
                ev_rows.append([v["scenario_no"], Path(ev["file_path"]).name,
                                ev["lock_kind"], ev["lock_detail"][:70], f"`{ev['sha256'][:24]}…`"])
    doc.table(["Scenario", "Artefact", "Lock verified", "Lock detail", "SHA-256 (prefix)"], ev_rows)
    if rejected:
        doc.h(3, "Rejected captures (refused by the evidence guard)")
        doc.p("These frames were deliberately **not** admitted as evidence. Their presence here is "
              "the audit trail proving the guard worked.")
        doc.table(["Scenario", "Rejection reason", "Detail"], rejected)

    doc.rule()
    doc.h(2, "8. Declaration")
    doc.p(f"The results in this report were produced by automated execution against "
          f"{device['model']} (firmware {device['firmware']}) using the Job IDs recorded in the "
          f"assessment database. Every verdict is traceable to a job, a plan version, and "
          f"stored evidence where available. Independent verification is required.")
    doc.table(["Role", "Name", "Date"], [["Assessor", "", ""], ["Reviewer", "", ""],
                                         ["Approver", "", ""]])

    slug = f"ITSAR_report_{device['model']}_{device['firmware']}".replace(" ", "").replace(".", "-")
    md_path = out_dir / f"{slug}.md"
    html_path = out_dir / f"{slug}.html"
    json_path = out_dir / f"{slug}.json"
    md_path.write_text(doc.to_markdown(), encoding="utf-8")
    html_path.write_text(doc.to_html(), encoding="utf-8")
    json_path.write_text(json.dumps({
        "device": {k: device[k] for k in ("model", "firmware", "host", "mgmt_protocol")},
        "generated": utcnow(), "counts": counts, "compliance_pct": round(pct, 1),
        "verdicts": [{k: v[k] for k in ("clause_no", "scenario_no", "result", "job_id", "notes")}
                     for v in verdicts],
        "scripts": [{k: s[k] for k in ("id", "device_id", "scenario_id", "version", "file_path")}
                    for s in scripts],
        "rejected_captures": len(rejected),
    }, indent=2), encoding="utf-8")
    return {"markdown": str(md_path), "html": str(html_path), "json": str(json_path),
            "counts": counts, "compliance_pct": round(pct, 1)}
