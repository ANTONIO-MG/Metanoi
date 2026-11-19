# Metanoia Group — upload format (`metanoia-upload/1`)

Every document you upload travels with two small files that share its name:

```
CGAU_B26S_T1_W1_Controls_StudyGuide_v1.pdf    the document students download (optional for AI-written / md-only)
CGAU_B26S_T1_W1_Controls_StudyGuide_v1.json   WHERE it goes  — the sidecar
CGAU_B26S_T1_W1_Controls_StudyGuide_v1.md     WHAT it says   — the text, in the template for its type
```

Upload all three together at **Learning Hub → Upload content**. You can select the files of
several documents at once; they are matched by name.

## What happens next

1. **Placed.** The sidecar decides the institution, programme, module, intake, which test or
   exam it prepares for, and which week.
2. **Drafted.** If the `.md` follows the template for its type, it is converted exactly — a
   reading lesson, or a quiz — with no AI involved. If it does not (or there is no `.md`),
   the AI assistant drafts the same thing from the document.
3. **Reviewed.** The draft waits in **Content queue**. You see what students will see, every
   problem (marks that do not add up, a question with no marking guide), and you can correct it.
4. **Approved.** It joins the module schedule as a draft only staff can see.
5. **Published.** One click opens it to the intake.

Nothing reaches a student without steps 4 and 5.

## The sidecar (`.json`)

```json
{
  "format": "metanoia-upload/1",
  "doc_type": "study_guide",
  "title": "Week 1 · Controls — Study Guide",
  "institution": "MILPARK",
  "programme": "BCTA",
  "module": "CGAU",
  "cohort": "B26S",
  "assessment": {"kind": "test", "sequence": 1},
  "week": 1,
  "week_title": "Week 1 · Controls",
  "topic": {"code": "T3", "title": "Controls"},
  "audience": "students",
  "used_on": "2026-08-04",
  "notes": "Anything the reviewer should know. Never shown to students."
}
```

| Field | Required | Values |
|---|---|---|
| `doc_type` | yes | `study_guide` · `blueprint` · `questions` · `mock_exam` · `past_paper` · `answers` · `planner` · `recording` · `reference` |
| `title` | yes | What students see. |
| `institution` | yes | `MILPARK` · `UNISA` · `IAS` · `SAICA` · `NMU` |
| `programme` | yes | `BCTA` · `PGDA` · `IAC` … |
| `module` | yes | `CGAU` · `MACF` · `FREP` · `TAX` (or the offering code, e.g. `TAXA`) |
| `cohort` | no | The intake: `B26F` · `B26S` · `P26F` · `P26S`. Leave out = every intake. |
| `assessment` | yes | `{"kind": "test", "sequence": 2}` · `{"kind": "exam", "sequence": 1}` (the Final) · `{"kind": "other", "sequence": 1}` (module resources) |
| `week` | no | The topic week inside that test's preparation. Leave out = the "Revision", "Mock exams" or "Past papers" week, by type. |
| `week_title` | no | e.g. `"Week 2 · Companies Act s45/s46/s75"` |
| `topic` | no | `{"code": "T3", "title": "Controls"}` |
| `audience` | no | `students` (default) or `staff` — staff-only material can never be published to students. |
| `used_on` | no | `YYYY-MM-DD` — the date it was used with the class. |
| `marks`, `minutes` | papers | Total marks and the time limit for a mock. |
| `video_url` | recordings | A YouTube / Teams recording link — becomes a "Watch recording" button. |
| `live_url` | live | A meeting link — becomes a "Join live session" button. |

## The text (`.md`) — one template per type

* **Study guide / blueprint** — `# Title`, then one `## Heading` per section. Ordinary
  Markdown inside (lists, **bold**, tables). A line `Video: https://…` under a heading puts that
  recording at the top of the section. Reading time is worked out for you (200 words a minute).
* **Questions & answers / mock exam** — `# Title`, a `## Scenario` (and any other case
  sections), then one `## Question (a) — 12 marks` per REQUIRED part with:
  * `### Required` — what the candidate must do;
  * `### Marking guide` — a table `| # | Answer point | Authority | Marks |`, one row per point
    that earns marks (the rows must add up to the part's marks); **or**
  * `### Marking guide (computation)` — `| Line | Authority | Amount | Marks |` for a figure
    marked automatically; **or**
  * `### Options` — `- [x] correct option` / `- [ ] wrong option` for multiple choice
    (heading `## Question 3 — 1 mark — MCQ`), with an optional `### Explanation`.
* **Past paper / answers / reference** — no `.md` needed: the document is placed as a file.
* **Planner** — a table whose rows start with a date: `| 17 Aug 2026 | CGAU Test 1 | test |`.
  Each row becomes a calendar entry for the intake (kinds: test, exam, session, mock, deadline, other).
* **Recording / live session** — no `.md` needed: put `video_url` or `live_url` in the sidecar.

## Naming

`<MODULE>_<INTAKE>_<T1|T2|T3|T4|Final>_<W1…>_<What>_v<n>.<ext>` — e.g.
`CGAU_B26S_T2_W1_CompaniesAct_StudyGuide_v1.pdf`. The name is a fallback: if a sidecar field is
missing, the system reads the module, intake, test and week from the name.

Samples for every type are in the folders beside this file, and downloadable from
**Learning Hub → Upload format & samples**.
